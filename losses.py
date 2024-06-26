# Based on SLIP code bases
# https://github.com/facebookresearch/SLIP
# --------------------------------------------------------
import torch
import torch.nn as nn
import torch.nn.functional as F

import utils

def get_metric_names():
    metics = ["loss"]
    metics.extend(["simclr_loss","im_byol_loss","contra_loss_1","contra_loss_2","clip_acc"])

    return metics


def cal_simsiam_loss(p, z, version="simplified"):  # negative cosine similarity
    if version == "original":
        z = z.detach()  # stop gradient
        p = F.normalize(p, dim=1)  # l2-normalize
        z = F.normalize(z, dim=1)  # l2-normalize
        return -(p * z).sum(dim=1).mean()

    elif (
        version == "simplified"
    ):  # same thing, much faster. Scroll down, speed test in __main__
        return -F.cosine_similarity(p, z.detach(), dim=-1).mean()
    else:
        raise Exception


class ACLIPLoss(nn.Module):
    def __init__(self, temperature=0.1):
        super().__init__()
        self.labels = None
        self.last_local_batch_size = None
        self.simclr_loss = SIMCLRLoss(temperature=temperature)

    def forward(self, outputs):
        image_embed = outputs["image_embed"]
        text_embed = outputs["text_embed"]
        logit_scale = outputs["logit_scale"]

        # cal simclr_loss
        bs = text_embed.shape[0]
        image_ssl_embed = outputs["image_ssl_embed"]
        inputs = {}
        inputs["aug1_embed"] = image_ssl_embed[:bs]
        inputs["aug2_embed"] = image_ssl_embed[bs:]
        simclr_loss_dict = self.simclr_loss(inputs)

        def loss_fn(x, y):
            x = F.normalize(x, dim=-1, p=2)
            y = F.normalize(y, dim=-1, p=2)
            return 2 - 2 * (x * y).sum(dim=-1)

        im_features = outputs["byol_feats"]
        im_features_e = outputs["byol_feats_e"]
        im_features_e = torch.cat([im_features_e, im_features_e], dim=0)
        im_byol_loss = loss_fn(im_features, im_features_e).mean()

        local_batch_size = text_embed.size(0)

        if local_batch_size != self.last_local_batch_size:
            self.labels = local_batch_size * utils.get_rank() + torch.arange(
                local_batch_size, device=image_embed.device
            )
            self.last_local_batch_size = local_batch_size

        image_embed = F.normalize(image_embed, dim=-1, p=2)
        text_embed = F.normalize(text_embed, dim=-1, p=2)

        image_embed_1 = image_embed[:local_batch_size]
        image_embed_2 = image_embed[local_batch_size:]

        (
            image_embed_all_1,
            image_embed_all_2,
            text_embed_all,
        ) = utils.all_gather_batch_with_grad([image_embed_1, image_embed_2, text_embed])

        # cosine similarity as logits
        logits_per_image = logit_scale * image_embed_1 @ text_embed_all.t()
        logits_per_text = logit_scale * text_embed @ image_embed_all_1.t()

        contra_loss_1 = (
            F.cross_entropy(logits_per_image, self.labels)
            + F.cross_entropy(logits_per_text, self.labels)
        ) / 2

        logits_per_image = logit_scale * image_embed_2 @ text_embed_all.t()
        logits_per_text = logit_scale * text_embed @ image_embed_all_2.t()

        contra_loss_2 = (
            F.cross_entropy(logits_per_image, self.labels)
            + F.cross_entropy(logits_per_text, self.labels)
        ) / 2
       

        loss = (
            0.5 * contra_loss_1
            + 0.5 * contra_loss_2
            + simclr_loss_dict["ssl_loss"]
            + 2 * im_byol_loss
        )

        # compute accuracy
        with torch.no_grad():
            pred = torch.argmax(logits_per_image, dim=-1)
            correct = pred.eq(self.labels).sum()
            acc = 100 * correct / local_batch_size

        return {
            "loss": loss,
            "simclr_loss": simclr_loss_dict["ssl_loss"],
            "im_byol_loss": im_byol_loss,
            "contra_loss_1": contra_loss_1,
            "contra_loss_2": contra_loss_2,
            "clip_acc": acc,
        }
      

class SIMCLRLoss(nn.Module):
    """
    This is the SimCLR loss in https://arxiv.org/abs/2002.05709
    The embedding vectors are assumed to have size (2 x batch_size, embedding_dim) and
    the memory layout that can be reshaped into shape (2, batch_size, embedding_dim).
    This memory layout is consistent with the SimCLR collator in
    https://github.com/facebookresearch/vissl/blob/master/vissl/data/collators/simclr_collator.py
    Config params:
        temperature (float): the temperature to be applied on the logits
    """

    def __init__(self, temperature=0.1):
        super().__init__()
        self.tau = temperature
        self.labels = None
        self.masks = None
        self.last_local_batch_size = None

    def forward(self, outputs):
        q_a = outputs["aug1_embed"]
        q_b = outputs["aug2_embed"]

        q_a = F.normalize(q_a, dim=-1, p=2)
        q_b = F.normalize(q_b, dim=-1, p=2)

        local_batch_size = q_a.size(0)

        k_a, k_b = utils.all_gather_batch_with_grad([q_a, q_b])

        if local_batch_size != self.last_local_batch_size:
            self.labels = local_batch_size * utils.get_rank() + torch.arange(
                local_batch_size, device=q_a.device
            )
            total_batch_size = local_batch_size * utils.get_world_size()
            self.masks = F.one_hot(self.labels, total_batch_size) * 1e9
            self.last_local_batch_size = local_batch_size

        logits_aa = torch.matmul(q_a, k_a.transpose(0, 1)) / self.tau
        logits_aa = logits_aa - self.masks
        logits_bb = torch.matmul(q_b, k_b.transpose(0, 1)) / self.tau
        logits_bb = logits_bb - self.masks
        logits_ab = torch.matmul(q_a, k_b.transpose(0, 1)) / self.tau
        logits_ba = torch.matmul(q_b, k_a.transpose(0, 1)) / self.tau

        loss_a = F.cross_entropy(torch.cat([logits_ab, logits_aa], dim=1), self.labels)
        loss_b = F.cross_entropy(torch.cat([logits_ba, logits_bb], dim=1), self.labels)
        loss = (loss_a + loss_b) / 2  # divide by 2 to average over all samples

        # compute accuracy
        with torch.no_grad():
            pred = torch.argmax(torch.cat([logits_ab, logits_aa], dim=1), dim=-1)
            correct = pred.eq(self.labels).sum()
            acc = 100 * correct / local_batch_size

        return {"loss": loss, "ssl_loss": loss, "ssl_acc": acc}
