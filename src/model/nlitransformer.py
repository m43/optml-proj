import json
import os
from abc import ABCMeta, abstractmethod

import numpy as np
import pandas as pd
import torch
import wandb
from pytorch_lightning import LightningModule
from pytorch_lightning.loggers import WandbLogger
from torch.nn import functional as F
from torch.optim import AdamW, Adam
from transformers import AutoModelForSequenceClassification, BertForSequenceClassification, PreTrainedTokenizerBase, \
    AutoTokenizer, get_linear_schedule_with_warmup, get_polynomial_decay_schedule_with_warmup
from transformers import T5ForConditionalGeneration
from transformers.modeling_outputs import SequenceClassifierOutput

from src.constants import *
from src.model.focalloss import FocalLoss
from src.utils.util import get_logger

T5_ENTAILMENT_LABEL = torch.tensor([3, 35, 5756, 297, 1])  # 0
T5_NEUTRAL_LABEL = torch.tensor([7163, 1, 0, 0, 0])  # 1
T5_CONTRADICTION_LABEL = torch.tensor([27252, 1, 0, 0, 0])  # 2

log = get_logger(__name__)


class HuggingFaceTransformerForNLI(LightningModule, metaclass=ABCMeta):
    """
    A PyTorch Lightning module that is a wrapper around
    a HuggingFace transformer.

    Other than wrapping a transformer, this class provides the
    functionality of training on MultiNLI dataset and evaluating
    on MultiNLI and HANS dataset. It also adds verbose logging.

    The module uses a linear warmup of configurable length
    and can be configured to either use a polynomial
    or a linear learning rate decay schedule.
    """

    model_name: str

    def __init__(self):
        super().__init__()
        self.transformer = None
        self.loss_criterion = None

    @abstractmethod
    def forward(self, *args, **kwargs):
        pass

    def training_step(self, batch, batch_idx):
        results = self._step(batch)
        self._step_log(batch, batch_idx, "Train", results)
        return results

    def validation_step(self, batch, batch_idx, dataloader_idx=0):
        results = self._step(batch)
        self._step_log(batch, batch_idx, "Valid", results, dataloader_idx)
        return results

    def _step_log(self, batch, batch_idx, prefix, results, dataloader_idx=0):
        dataset = results["datapoint_dataset"][0].item()
        dataset_str = INTEGER_TO_DATASET[dataset]

        if batch_idx == 0 or batch_idx == -1 and self.global_rank == 0 and self.current_epoch in [0, 1]:
            self._log_batch_for_debugging(f"{prefix}/Batch/batch-{batch_idx}_dataloader-{dataloader_idx}", batch)

        if dataset in MNLI_DATASET_INTEGER_IDENTIFIERS + SNLI_DATASET_INTEGER_IDENTIFIERS:
            log_kwargs = {
                'prog_bar': True,
                'add_dataloader_idx': False,
            }
            self.log(f"{prefix}/{dataset_str}/loss_step", results["loss"], **log_kwargs)
            self.log(f"{prefix}/{dataset_str}/acc_step", results["acc"], **log_kwargs)
            self.log(f"{prefix}/{dataset_str}/datapoint_count_step", results["count"], **log_kwargs)

    def _log_batch_for_debugging(self, log_key, batch):
        def jsonify(value):
            if isinstance(value, torch.Tensor):
                return value.tolist()
            return value

        debug_tokenizer: PreTrainedTokenizerBase = AutoTokenizer.from_pretrained(PRETRAINED_IDS[self.model_name])
        batch = dict(batch)  # do not modify the original batch dict
        batch["txt"] = debug_tokenizer.batch_decode(batch["input_ids"])

        batch_json = json.dumps({k: jsonify(v) for k, v in batch.items()})
        log.info(f"{log_key}:\n{batch_json}")

        batch_df = pd.DataFrame({k: [str(jsonify(e)) for e in v] for k, v in batch.items()})
        for logger in self.loggers:
            if isinstance(logger, WandbLogger):
                logger: WandbLogger = logger
                logger.log_text(f"{log_key}", dataframe=batch_df)

    def _epoch_end(self, split: str, results_list):
        dataset = results_list[0]["datapoint_dataset"][0].item()
        dataset_str = INTEGER_TO_DATASET[dataset]

        # Parse results from the results list
        results = {}
        for k in results_list[0].keys():
            if not k.startswith("datapoint"):
                # We are not interested in accumulated batch metrics
                # (avg loss per batch, avg acc per batch, etc.)
                # since we will recompute them with all datapoints
                continue
            values = [x[k] for x in results_list]
            results[k] = torch.cat(values).detach().cpu().numpy()

        # Add additional information to the results
        n = len(results["datapoint_idx"])
        assert n == len(results["datapoint_loss"]) == len(results["datapoint_true_pred"])
        results["epoch"] = np.repeat(self.current_epoch, n)
        results["step"] = np.repeat(self.global_step, n)
        results["datapoint_heuristics_str"] = np.array([
            INTEGER_TO_HEURISTIC[h]
            for h in results["datapoint_heuristic"]
        ])
        results["datapoint_handcrafted_type_str"] = np.array([
            HandcraftedType(t).name.title()
            for t in results["datapoint_handcrafted_type"]
        ])

        # Add selected logs to logger with self.log (otherwise, all logs saved in the dataframe)
        loss = results["datapoint_loss"].mean(dtype=np.float64)
        acc = results["datapoint_true_pred"].mean(dtype=np.float64)
        assert acc == (results["datapoint_pred"] == results["datapoint_label"]).mean()

        log_kwargs = {
            'prog_bar': True,
            'add_dataloader_idx': False,
        }
        self.log(f"{split}/{dataset_str}/loss_epoch", loss, **log_kwargs)
        self.log(f"{split}/{dataset_str}/acc_epoch", acc, **log_kwargs)
        self.log(f"{split}/{dataset_str}/datapoint_count_epoch", float(n), **log_kwargs)

        # Additional logs per dataset
        if dataset in MNLI_DATASET_INTEGER_IDENTIFIERS + SNLI_DATASET_INTEGER_IDENTIFIERS:
            self._log_mnli_epoch_end(split, dataset_str, results)
        if dataset in HANS_DATASET_INTEGER_IDENTIFIERS:
            self._log_hans_epoch_end(split, dataset_str, results)

        # Create a DataFrame to be used in post-run logs processing to create visuals for the paper report
        for k in results.keys():
            if results[k].ndim > 1:  # Data for a pd.DataFrame must be 1-dimensional
                results[k] = [str(x) for x in results[k]]
        df = pd.DataFrame(results)

        # Log the dataframe to wandb
        for logger in self.loggers:
            if isinstance(logger, WandbLogger):
                logger: WandbLogger = logger
                csv_path = os.path.join(
                    logger.experiment.dir,
                    f"{split}_{dataset_str}_epoch_end_df_epoch-{self.current_epoch}_step-{self.global_step}.csv"
                )
                df.to_csv(csv_path)
                artifact = wandb.Artifact(
                    name=f"{logger.experiment.name}-{split}-{dataset_str}_epoch_end_df",
                    type="df",
                    metadata={"epoch": self.current_epoch, "step": self.global_step},
                )
                artifact.add_file(csv_path, "df.csv")
                logger.experiment.log_artifact(artifact)

    def training_epoch_end(self, outputs):
        results_list = outputs
        self._epoch_end("Train", results_list)

    def validation_epoch_end(self, outputs):
        for results_list in outputs:
            self._epoch_end("Valid", results_list)

    def _log_mnli_epoch_end(self, prefix, dataset_str, results):
        handcrafted_types = results["datapoint_handcrafted_type"],
        losses = results["datapoint_loss"],
        true_preds = results["datapoint_true_pred"],

        for handcrafted_type in HandcraftedType:
            mask = handcrafted_types == handcrafted_type.value
            loss_per_type = losses[mask].mean()
            acc_per_type = true_preds[mask].mean()
            log_kwargs = {
                'on_step': False,
                'on_epoch': True,
                'prog_bar': True,
                'logger': True,
                'add_dataloader_idx': False,
            }
            self.log(f"{prefix}/HandcraftedType/{dataset_str}_{handcrafted_type.name.lower()}_loss", loss_per_type,
                     **log_kwargs)
            self.log(f"{prefix}/HandcraftedType/{dataset_str}_{handcrafted_type.name.lower()}_accuracy", acc_per_type,
                     **log_kwargs)

    def _log_hans_epoch_end(self, prefix, dataset_str, results):
        log_kwargs = {
            'add_dataloader_idx': False,
        }

        heuristics = results["datapoint_heuristic"]
        labels = results["datapoint_label"]
        losses = results["datapoint_loss"]
        preds = results["datapoint_pred"]

        for target_label, label_description in enumerate(["entailment", "non_entailment"]):
            for heuristic_name, heuristic_idx in HEURISTIC_TO_INTEGER.items():
                mask = (heuristics == heuristic_idx) & (labels == target_label)
                if mask.sum() == 0:
                    # This might be true during the sanity checks of PyTorch Lightning
                    # where not the whole dataset would be used, but a small subset.
                    # This way we avoid NaN and polluting our metrics.
                    continue

                loss = losses[mask].mean()
                acc = (preds[mask] == labels[mask]).mean()
                self.log(f"{prefix}/Hans_loss/{label_description}__{heuristic_name}", loss, **log_kwargs)
                self.log(f"{prefix}/Hans_acc/{label_description}__{heuristic_name}", acc, **log_kwargs)

    @abstractmethod
    def configure_optimizers(self):
        pass


class T5ForNLI(HuggingFaceTransformerForNLI):
    """
    A PyTorch Lightning module that is a wrapper around
    a HuggingFace T5 for conditional generation model.
    """

    model_name = T5_IDENTIFIER

    def __init__(self, **kwargs):
        super().__init__()
        self.save_hyperparameters()
        print("-" * 72)
        print(f"self.hparams={self.hparams}")
        print("-" * 72)

        self.transformer: T5ForConditionalGeneration = T5ForConditionalGeneration.from_pretrained(
            PRETRAINED_IDS[self.model_name])
        print(self.transformer.config)

        assert isinstance(self.transformer, T5ForConditionalGeneration)

        # initialized in self.setup()
        self.loss_criterion = FocalLoss(self.hparams.focal_loss_gamma)

    def forward(self, input_ids, attention_mask, target_input_ids, target_attention_mask, **kwargs):
        output = self.transformer.forward(
            input_ids=input_ids,
            attention_mask=attention_mask,
            labels=target_input_ids,
            decoder_attention_mask=target_attention_mask,
            return_dict=True
        )
        return output

    def _step(self, batch):
        output = self.forward(**batch)
        batch_size = batch['target_input_ids'].size(0)

        loss = self.loss_criterion(output.logits.view(-1, output.logits.size(-1)), batch["target_input_ids"].view(-1))
        loss = loss.resize(batch_size, T5_LABEL_PAD_LENGTH).mean(-1)

        pred = torch.full((batch_size,), -1)
        prob = torch.full((batch_size, 3), -1)

        pred_tmp = output.logits.argmax(dim=-1)
        true_pred = (pred_tmp == batch['target_input_ids']).all(dim=1).float()
        prob_tmp = output.logits.softmax(-1).detach().clone()
        true_prob = prob_tmp.gather(-1, batch['target_input_ids'].unsqueeze(-1)).squeeze(-1).prod(-1)

        prob = prob.prod(-1)

        # **********************************************************************
        # HANS labels: entailment=0, non-entailment=1
        # MNLI, SNLI labels: entailment=0, neutral=1, contradiction=2
        # We map neutral+contradiction to non-entailment, as done in literature:
        #   McCoy et al.: https://arxiv.org/abs/1902.01007
        #   Clark et al.: https://aclanthology.org/D19-1418/
        dataset = batch["dataset"][0].item()
        if dataset in HANS_DATASET_INTEGER_IDENTIFIERS:
            raise NotImplementedError()
        # **********************************************************************

        if "heuristic" in batch:  # HANS has the heuristic type, MNLI does not
            heuristic = batch["heuristic"]
        else:
            heuristic = true_pred.new_ones(true_pred.shape) * -1.0

        results = {
            "loss": loss.mean(),
            "acc": true_pred.mean(),
            "count": float(len(pred)),
            "datapoint_idx": batch["idx"],
            "datapoint_dataset": batch["dataset"],
            "datapoint_label": batch["labels"],
            "datapoint_handcrafted_type": batch["handcrafted_type"],
            "datapoint_heuristic": heuristic,
            "datapoint_loss": loss.detach().clone(),
            "datapoint_pred": pred,
            "datapoint_true_pred": true_pred,
            "datapoint_prob": prob,
            "datapoint_true_prob": true_prob,
        }
        return results

    def configure_optimizers(self):
        """Prepare optimizer and schedule (linear warmup and decay)"""

        model = self.transformer
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "name": "1_w-decay",
                "params": [
                    p for n, p in model.named_parameters()
                    if not any(nd in n for nd in no_decay)
                ],
                "weight_decay": self.hparams.weight_decay,
            },
            {
                "name": "2_no-decay",
                "params": [
                    p for n, p in model.named_parameters()
                    if any(nd in n for nd in no_decay)
                ],
                "weight_decay": 0.0,
            },
        ]
        if self.hparams.optimizer_name == "adamw":
            optimizer = AdamW(
                optimizer_grouped_parameters,
                lr=self.hparams.learning_rate,
                weight_decay=self.hparams.weight_decay,
                eps=self.hparams.adam_epsilon,
            )
        elif self.hparams.optimizer_name == "adam":
            optimizer = Adam(
                optimizer_grouped_parameters,
                lr=self.hparams.learning_rate,
                weight_decay=self.hparams.weight_decay,
                eps=self.hparams.adam_epsilon,
            )
        else:
            raise ValueError(f"Invalid optimizer_name given: {self.hparams.optimizer_name}")

        train_steps = self.trainer.estimated_stepping_batches

        if self.hparams.warmup_ratio is not None and self.hparams.warmup_steps is not None:
            raise ValueError("Either warmup_steps or warmup_ratio should be given, but not both.")

        if self.hparams.warmup_steps:
            warmup_steps = self.hparams.warmup_steps
        elif self.hparams.warmup_ratio:
            warmup_steps = train_steps * self.hparams.warmup_ratio
        else:
            raise ValueError("Either warmup_steps or warmup_ratio should be given, but none were given.")

        if self.hparams.scheduler_name == "linear":
            scheduler = get_linear_schedule_with_warmup(
                optimizer,
                num_warmup_steps=warmup_steps,
                num_training_steps=train_steps,
            )
            scheduler = {"scheduler": scheduler, "interval": "step", "frequency": 1}
        elif self.hparams.scheduler_name == "polynomial":
            scheduler = get_polynomial_decay_schedule_with_warmup(
                optimizer,
                num_warmup_steps=warmup_steps,
                num_training_steps=train_steps,
                lr_end=0.0,
            )
            scheduler = {"scheduler": scheduler, "interval": "step", "frequency": 1}
        else:
            raise ValueError(f"Invalid scheduler_name given: {self.hparams.optimizer_name}")

        return [optimizer], [scheduler]


class BertForNLI(HuggingFaceTransformerForNLI):
    """
    A PyTorch Lightning module that is a wrapper around
    a HuggingFace BERT for sequence classification model.
    """

    model_name = BERT_IDENTIFIER

    def __init__(self, **kwargs):
        super().__init__()
        self.save_hyperparameters()
        print("-" * 72)
        print(f"self.hparams={self.hparams}")
        print("-" * 72)

        self.transformer: BertForSequenceClassification = AutoModelForSequenceClassification.from_pretrained(
            PRETRAINED_IDS[self.model_name],
            hidden_dropout_prob=self.hparams["hidden_dropout_prob"],
            attention_probs_dropout_prob=self.hparams["attention_probs_dropout_prob"],
            classifier_dropout=self.hparams["classifier_dropout"],
            num_labels=3,
        )
        print(self.transformer.config)

        assert isinstance(self.transformer, BertForSequenceClassification)

        # initialized in self.setup()
        self.loss_criterion = FocalLoss(self.hparams.focal_loss_gamma)

    def forward(self, input_ids, attention_mask, token_type_ids, label=None, **kwargs) -> SequenceClassifierOutput:
        output: SequenceClassifierOutput = self.transformer.forward(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids
        )
        return output

    def _step(self, batch):
        output = self.forward(**batch)

        # Compute loss
        onehot_labels = F.one_hot(batch["labels"], num_classes=3).float()

        # **********************************************************************
        # HANS labels: entailment=0, non-entailment=1
        # MNLI, SNLI labels: entailment=0, neutral=1, contradiction=2
        # We map sum up neutral+contradiction scores to get non-entailment.
        # McCoy et al. (https://arxiv.org/abs/1902.01007) have done likewise when augmenting MNLI with HANS examples.
        prob_raw = output.logits.softmax(-1)

        prob_entailment = prob_raw[:, 0]
        prob_neutral = prob_raw[:, 1] + (batch["dataset"] == DATASET_TO_INTEGER["hans_train"]) * prob_raw[:, 2]
        prob_contradiction_raw = (batch["dataset"] != DATASET_TO_INTEGER["hans_train"]) * prob_raw[:, 2]
        prob_contradiction_fix_nan = (batch["dataset"] == DATASET_TO_INTEGER["hans_train"]) * 1e-9
        prob_contradiction = prob_contradiction_raw + prob_contradiction_fix_nan

        prob = torch.stack([prob_entailment, prob_neutral, prob_contradiction], dim=-1)

        logits = prob.log()
        # **********************************************************************

        loss = self.loss_criterion(logits, onehot_labels)

        # Compute prediction probability
        #  - prob: probability for individual classes
        #  - true_prob: probability for the correct class
        # **********************************************************************
        # HANS labels: entailment=0, non-entailment=1
        # MNLI, SNLI labels: entailment=0, neutral=1, contradiction=2
        # We map neutral+contradiction to non-entailment, as done in literature:
        #   McCoy et al.: https://arxiv.org/abs/1902.01007
        #   Clark et al.: https://aclanthology.org/D19-1418/
        dataset = batch["dataset"][0].item()
        if dataset == DATASET_TO_INTEGER["hans_validation"]:
            # pred = output.logits.argmax(dim=-1)
            prob[:, 1] += prob[:, 2]
            prob = prob[:, :2]
        # **********************************************************************
        true_prob = prob.gather(-1, batch["labels"].unsqueeze(-1)).squeeze(-1)

        # Compute the prediction
        #  - pred: what class do we predict (e.g. entailment=0)
        #  - true_pred: did we predict the correct class (no=0, yes=1)
        pred = prob.argmax(dim=-1)
        true_pred = (pred == batch["labels"]).float()

        # Extract the heuristic type. Only HANS has the heuristic type (e.g. lexical_overlap), for others we put -1
        if "heuristic" in batch:
            heuristic = batch["heuristic"]
        else:
            heuristic = true_pred.new_ones(true_pred.shape) * -1.0

        results = {
            "loss": loss.mean(),
            "acc": true_pred.mean(),
            "count": float(len(pred)),
            "datapoint_idx": batch["idx"],
            "datapoint_dataset": batch["dataset"],
            "datapoint_label": batch["labels"],
            "datapoint_handcrafted_type": batch["handcrafted_type"],
            "datapoint_heuristic": heuristic,
            "datapoint_loss": loss.detach().clone(),
            "datapoint_pred": pred,
            "datapoint_true_pred": true_pred,
            "datapoint_prob": prob,
            "datapoint_true_prob": true_prob,
        }
        return results

    def configure_optimizers(self):
        """Prepare optimizer and schedule (linear warmup and decay)"""

        model = self.transformer
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "name": "1_w-decay",
                "params": [
                    p for n, p in model.named_parameters()
                    if not any(nd in n for nd in no_decay)
                ],
                "weight_decay": self.hparams.weight_decay,
            },
            {
                "name": "2_no-decay",
                "params": [
                    p for n, p in model.named_parameters()
                    if any(nd in n for nd in no_decay)
                ],
                "weight_decay": 0.0,
            },
        ]
        if self.hparams.optimizer_name == "adamw":
            optimizer = AdamW(
                optimizer_grouped_parameters,
                lr=self.hparams.learning_rate,
                weight_decay=self.hparams.weight_decay,
                eps=self.hparams.adam_epsilon,
            )
        elif self.hparams.optimizer_name == "adam":
            optimizer = Adam(
                optimizer_grouped_parameters,
                lr=self.hparams.learning_rate,
                weight_decay=self.hparams.weight_decay,
                eps=self.hparams.adam_epsilon,
            )
        else:
            raise ValueError(f"Invalid optimizer_name given: {self.hparams.optimizer_name}")

        train_steps = self.trainer.estimated_stepping_batches

        if self.hparams.warmup_ratio is not None and self.hparams.warmup_steps is not None:
            raise ValueError("Either warmup_steps or warmup_ratio should be given, but not both.")

        if self.hparams.warmup_steps is not None:
            warmup_steps = self.hparams.warmup_steps
        elif self.hparams.warmup_ratio is not None:
            warmup_steps = train_steps * self.hparams.warmup_ratio
        else:
            raise ValueError("Either warmup_steps or warmup_ratio should be given, but none were given.")

        if self.hparams.scheduler_name == "linear":
            scheduler = get_linear_schedule_with_warmup(
                optimizer,
                num_warmup_steps=warmup_steps,
                num_training_steps=train_steps,
            )
            scheduler = {"scheduler": scheduler, "interval": "step", "frequency": 1}
        elif self.hparams.scheduler_name == "polynomial":
            scheduler = get_polynomial_decay_schedule_with_warmup(
                optimizer,
                num_warmup_steps=warmup_steps,
                num_training_steps=train_steps,
                lr_end=0.0,
            )
            scheduler = {"scheduler": scheduler, "interval": "step", "frequency": 1}
        else:
            raise ValueError(f"Invalid scheduler_name given: {self.hparams.optimizer_name}")

        return [optimizer], [scheduler]
