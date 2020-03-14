# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and
# The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Finetuning the library models for sequence classification on TRAC
(Bert, XLM, XLNet, RoBERTa, Albert, XLM-RoBERTa)."""


import argparse
import glob
import json
import logging
import os
import random
import traceback

import numpy as np
import torch
from torch import nn
from torch.nn import CrossEntropyLoss, MSELoss
from torch.utils.data import (
    DataLoader,
    RandomSampler,
    SequentialSampler,
    TensorDataset,
)
from torch.utils.data.distributed import DistributedSampler

# Replaced this `from tqdm import tqdm, trange`
# !pip install --force https://github.com/chengs/tqdm/archive/colab.zip
from tqdm.autonotebook import tqdm, trange
from transformers import (
    WEIGHTS_NAME,
    AdamW,
    BertConfig,
    BertModel,
    BertPreTrainedModel,
    BertTokenizer,
    get_linear_schedule_with_warmup,
)

from trac_dataloader import (
    compute_metrics,
    convert_examples_to_features,
    output_modes,
    processors,
)

try:
    from torch.utils.tensorboard import SummaryWriter
except ImportError:
    from tensorboardX import SummaryWriter


logger = logging.getLogger(__name__)

MODEL_CLASSES = {"bert": (BertConfig, BertPreTrainedModel, BertTokenizer)}


class MultiHeadClassification(BertPreTrainedModel):
    r"""
        derived from BertForSequenceClassification
        **labels**: (`optional`) ``torch.LongTensor`` of shape
            ``(batch_size,)``:
            Labels for computing the sequence classification/regression loss.
            Indices should be in ``[0, ..., config.num_labels - 1]``.
            If ``config.num_labels == 1`` a regression loss is
                computed (Mean-Square loss),
            If ``config.num_labels > 1`` a classification loss is
                computed (Cross-Entropy).

    Outputs: `Tuple` comprising various elements depending on the configuration
            (config) and inputs:
        **loss**: (`optional`, returned when ``labels`` is provided)
                ``torch.FloatTensor`` of shape ``(1,)``:
            Classification (or regression if config.num_labels==1) loss.

        **logits**: ``torch.FloatTensor`` of shape
                ``(batch_size, config.num_labels)``
            Classification (or regression if config.num_labels==1) scores
                (before SoftMax).

        **hidden_states**: (`optional`, returned when
                ``config.output_hidden_states=True``)
            list of ``torch.FloatTensor``
                (one for the output of each layer +
                 the output of the embeddings)
            of shape ``(batch_size, sequence_length, hidden_size)``:
            Hidden-states of the model at the output of each layer plus
                the initial embedding outputs.
        **attentions**: (`optional`, returned when
                ``config.output_attentions=True``)
            list of ``torch.FloatTensor`` (one for each layer) of shape
                ``(batch_size, num_heads, sequence_length, sequence_length)``:
            Attentions weights after the attention softmax, used to compute
                the weighted average in the self-attention heads.

    Examples::

        tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
        model = BertForSequenceClassification.from_pretrained(
            'bert-base-uncased')
        input_ids = torch.tensor(
            tokenizer.encode(
                "Hello, my dog is cute")).unsqueeze(0)  # Batch size 1
        labels = torch.tensor([1]).unsqueeze(0)  # Batch size 1
        outputs = model(input_ids, labels=labels)
        loss, logits = outputs[:2]

    """

    def __init__(self, config):
        super().__init__(config)
        self.num_labels_a = config.num_labels_a
        self.num_labels_b = config.num_labels_b

        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier_a = nn.Linear(
            config.hidden_size, self.config.num_labels_a
        )
        self.classifier_b = nn.Linear(
            config.hidden_size, self.config.num_labels_b
        )

        self.init_weights()

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        labels_a=None,
        labels_b=None,
        *args,
        **kwargs,
    ):

        outputs = self.bert(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
        )

        pooled_output = outputs[1]

        pooled_output = self.dropout(pooled_output)
        logits_a = self.classifier_a(pooled_output)
        logits_b = self.classifier_b(pooled_output)

        outputs = (
            (logits_a,) + (logits_b,) + outputs[2:]
        )  # add hidden states and attention if they are here

        loss = 0
        for labels, logits, num_labels in (
            (labels_a, logits_a, self.num_labels_a),
            (labels_b, logits_b, self.num_labels_b),
        ):
            if labels is not None:
                if num_labels == 1:
                    #  We are doing regression
                    loss_fct = MSELoss()
                    loss += loss_fct(logits_a.view(-1), labels_a.view(-1))
                else:
                    loss_fct = CrossEntropyLoss()
                    loss += loss_fct(
                        logits.view(-1, num_labels), labels.view(-1)
                    )
        outputs = (loss,) + outputs
        return outputs  # (loss), logits, (hidden_states), (attentions)


def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


def train(args, train_dataset, model, tokenizer):
    """Train the model."""
    if args.local_rank in [-1, 0]:
        tb_writer = SummaryWriter()

    args.train_batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)
    train_sampler = (
        RandomSampler(train_dataset)
        if args.local_rank == -1
        else DistributedSampler(train_dataset)
    )
    train_dataloader = DataLoader(
        train_dataset, sampler=train_sampler, batch_size=args.train_batch_size
    )

    if args.max_steps > 0:
        t_total = args.max_steps
        args.num_train_epochs = (
            args.max_steps
            // (len(train_dataloader) // args.gradient_accumulation_steps)
            + 1
        )
    else:
        t_total = (
            len(train_dataloader)
            // args.gradient_accumulation_steps
            * args.num_train_epochs
        )

    # Prepare optimizer and schedule (linear warmup and decay)
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [
                p
                for n, p in model.named_parameters()
                if not any(nd in n for nd in no_decay)
            ],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [
                p
                for n, p in model.named_parameters()
                if any(nd in n for nd in no_decay)
            ],
            "weight_decay": 0.0,
        },
    ]

    optimizer = AdamW(
        optimizer_grouped_parameters,
        lr=args.learning_rate,
        eps=args.adam_epsilon,
    )
    scheduler = get_linear_schedule_with_warmup(
        optimizer,
        num_warmup_steps=args.warmup_steps,
        num_training_steps=t_total,
    )

    # Check if saved optimizer or scheduler states exist
    if os.path.isfile(
        os.path.join(args.model_name_or_path, "optimizer.pt")
    ) and os.path.isfile(
        os.path.join(args.model_name_or_path, "scheduler.pt")
    ):
        # Load in optimizer and scheduler states
        optimizer.load_state_dict(
            torch.load(os.path.join(args.model_name_or_path, "optimizer.pt"))
        )
        scheduler.load_state_dict(
            torch.load(os.path.join(args.model_name_or_path, "scheduler.pt"))
        )

    if args.fp16:
        try:
            from apex import amp
        except ImportError:
            raise ImportError(
                "Please install apex from "
                + "https://www.github.com/nvidia/apex to use fp16 training."
            )
        model, optimizer = amp.initialize(
            model, optimizer, opt_level=args.fp16_opt_level
        )

    # multi-gpu training (should be after apex fp16 initialization)
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Distributed training (should be after apex fp16 initialization)
    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(
            model,
            device_ids=[args.local_rank],
            output_device=args.local_rank,
            find_unused_parameters=True,
        )

    # Train!
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_dataset))
    logger.info("  Num Epochs = %d", args.num_train_epochs)
    logger.info(
        "  Instantaneous batch size per GPU = %d",
        args.per_gpu_train_batch_size,
    )
    logger.info(
        "  Total train batch size"
        + "(w. parallel, distributed & accumulation) = %d",
        args.train_batch_size
        * args.gradient_accumulation_steps
        * (torch.distributed.get_world_size() if args.local_rank != -1 else 1),
    )
    logger.info(
        "  Gradient Accumulation steps = %d", args.gradient_accumulation_steps
    )
    logger.info("  Total optimization steps = %d", t_total)

    global_step = 0
    epochs_trained = 0
    steps_trained_in_current_epoch = 0
    # Check if continuing training from a checkpoint
    if os.path.exists(args.model_name_or_path):
        # set global_step to global_step of last saved checkpoint
        # from model path
        try:
            global_step = int(
                args.model_name_or_path.split("-")[-1].split("/")[0]
            )
        except ValueError:
            global_step = 0
        epochs_trained = global_step // (
            len(train_dataloader) // args.gradient_accumulation_steps
        )
        steps_trained_in_current_epoch = global_step % (
            len(train_dataloader) // args.gradient_accumulation_steps
        )

        logger.info(
            "  Continuing training from checkpoint, will skip"
            + "to saved global_step"
        )
        logger.info("  Continuing training from epoch %d", epochs_trained)
        logger.info("  Continuing training from global step %d", global_step)
        logger.info(
            "  Will skip the first %d steps in the first epoch",
            steps_trained_in_current_epoch,
        )

    tr_loss, logging_loss = 0.0, 0.0
    model.zero_grad()
    train_iterator = trange(
        epochs_trained,
        int(args.num_train_epochs),
        desc="Epoch",
        disable=args.local_rank not in {-1, 0},
    )
    set_seed(args)  # Added here for reproductibility
    for _ in train_iterator:
        epoch_iterator = tqdm(
            train_dataloader,
            desc="Iteration",
            disable=args.local_rank not in {-1, 0},
        )
        for step, batch in enumerate(epoch_iterator):

            # Skip past any already trained steps if resuming training
            if steps_trained_in_current_epoch > 0:
                steps_trained_in_current_epoch -= 1
                continue

            model.train()
            batch = tuple(t.to(args.device) for t in batch)
            inputs = {
                "input_ids": batch[0],
                "attention_mask": batch[1],
                "labels_a": batch[3],
                "labels_b": batch[4],
            }
            if args.model_type != "distilbert":
                # XLM, DistilBERT, RoBERTa, and XLM-RoBERTa
                # don't use segment_ids
                inputs["token_type_ids"] = (
                    batch[2]
                    if args.model_type in ["bert", "xlnet", "albert"]
                    else None
                )
            outputs = model(**inputs)
            loss = outputs[
                0
            ]  # model outputs are always tuple in transformers (see doc)

            if args.n_gpu > 1:
                loss = (
                    loss.mean()
                )  # mean() to average on multi-gpu parallel training
            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps

            if args.fp16:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            tr_loss += loss.item()
            if (step + 1) % args.gradient_accumulation_steps == 0:
                if args.fp16:
                    torch.nn.utils.clip_grad_norm_(
                        amp.master_params(optimizer), args.max_grad_norm
                    )
                else:
                    torch.nn.utils.clip_grad_norm_(
                        model.parameters(), args.max_grad_norm
                    )

                optimizer.step()
                scheduler.step()  # Update learning rate schedule
                model.zero_grad()
                global_step += 1

                if (
                    args.local_rank in [-1, 0]
                    and args.logging_steps > 0
                    and global_step % args.logging_steps == 0
                ):
                    logs = {}
                    if (
                        args.local_rank == -1 and args.evaluate_during_training
                    ):  # Only evaluate when single GPU
                        # otherwise metrics may not average well
                        try:
                            results = evaluate(args, model, tokenizer)
                        except Exception as ex:
                            print(ex, "train-evaluate")
                            traceback.print_stack()
                        for key, value in results.items():
                            eval_key = "eval_{}".format(key)
                            logs[eval_key] = value

                    loss_scalar = (tr_loss - logging_loss) / args.logging_steps
                    learning_rate_scalar = scheduler.get_lr()[0]
                    logs["learning_rate"] = learning_rate_scalar
                    logs["loss"] = loss_scalar
                    logging_loss = tr_loss

                    for key, value in logs.items():
                        tb_writer.add_scalar(key, value, global_step)
                    print(json.dumps({**logs, **{"step": global_step}}))

                if (
                    args.local_rank in [-1, 0]
                    and args.save_steps > 0
                    and global_step % args.save_steps == 0
                ):
                    # Save model checkpoint
                    output_dir = os.path.join(
                        args.output_dir, "checkpoint-{}".format(global_step)
                    )
                    if not os.path.exists(output_dir):
                        os.makedirs(output_dir)
                    model_to_save = (
                        model.module if hasattr(model, "module") else model
                    )  # Take care of distributed/parallel training
                    model_to_save.save_pretrained(output_dir)
                    tokenizer.save_pretrained(output_dir)

                    torch.save(
                        args, os.path.join(output_dir, "training_args.bin")
                    )
                    logger.info("Saving model checkpoint to %s", output_dir)

                    torch.save(
                        optimizer.state_dict(),
                        os.path.join(output_dir, "optimizer.pt"),
                    )
                    torch.save(
                        scheduler.state_dict(),
                        os.path.join(output_dir, "scheduler.pt"),
                    )
                    logger.info(
                        "Saving optimizer and scheduler states to %s",
                        output_dir,
                    )

            if args.max_steps > 0 and global_step > args.max_steps:
                epoch_iterator.close()
                break
        if args.max_steps > 0 and global_step > args.max_steps:
            train_iterator.close()
            break

    if args.local_rank in [-1, 0]:
        tb_writer.close()

    return global_step, tr_loss / global_step


def evaluate(args, model, tokenizer, prefix=""):
    # Loop to handle MNLI double evaluation (matched, mis-matched)
    eval_task_names = (
        ("mnli", "mnli-mm") if args.task_name == "mnli" else (args.task_name,)
    )
    eval_outputs_dirs = (
        (args.output_dir, args.output_dir + "-MM")
        if args.task_name == "mnli"
        else (args.output_dir,)
    )
    results = {}
    for eval_task, eval_output_dir in zip(eval_task_names, eval_outputs_dirs):
        if args.do_eval:
            eval_dataset = load_and_cache_examples(
                args, eval_task, tokenizer, "dev"
            )
        else:
            eval_dataset = load_and_cache_examples(
                args, eval_task, tokenizer, "test"
            )

        if not os.path.exists(eval_output_dir) and args.local_rank in {-1, 0}:
            os.makedirs(eval_output_dir)

        args.eval_batch_size = args.per_gpu_eval_batch_size * max(
            1, args.n_gpu
        )
        # Note that DistributedSampler samples randomly
        eval_sampler = SequentialSampler(eval_dataset)
        eval_dataloader = DataLoader(
            eval_dataset, sampler=eval_sampler, batch_size=args.eval_batch_size
        )

        # multi-gpu eval
        if args.n_gpu > 1 and not isinstance(model, torch.nn.DataParallel):
            model = torch.nn.DataParallel(model)

        # Eval!
        logger.info("***** Running evaluation {} *****".format(prefix))
        logger.info("  Num examples = %d", len(eval_dataset))
        logger.info("  Batch size = %d", args.eval_batch_size)
        eval_loss = 0.0
        nb_eval_steps = 0
        preds = None
        out_label_ids = None
        for batch in tqdm(eval_dataloader, desc="Evaluating"):
            try:
                model.eval()
                batch = tuple(t.to(args.device) for t in batch)

                with torch.no_grad():
                    inputs = {
                        "input_ids": batch[0],
                        "attention_mask": batch[1],
                        "labels_a": batch[3],
                        "labels_b": batch[4],
                    }
                    if args.model_type != "distilbert":
                        # XLM, DistilBERT, RoBERTa,
                        # and XLM-RoBERTa don't use segment_ids
                        inputs["token_type_ids"] = (
                            batch[2]
                            if args.model_type in ["bert", "xlnet", "albert"]
                            else None
                        )
                    outputs = model(**inputs)
                    tmp_eval_loss, logits_a, logits_b = outputs[:3]

                    eval_loss += tmp_eval_loss.mean().item()
                nb_eval_steps += 1
                if preds is None:
                    preds_a = logits_a.detach().cpu().numpy()
                    preds_b = logits_b.detach().cpu().numpy()
                    out_label_ids_a = inputs["labels_a"].detach().cpu().numpy()
                    out_label_ids_b = inputs["labels_b"].detach().cpu().numpy()
                else:
                    preds_a = np.append(
                        preds_a, logits_a.detach().cpu().numpy(), axis=0
                    )
                    preds_b = np.append(
                        preds_b, logits_b.detach().cpu().numpy(), axis=0
                    )
                    out_label_ids_a = np.append(
                        out_label_ids_a,
                        inputs["labels_a"].detach().cpu().numpy(),
                        axis=0,
                    )
                    out_label_ids_b = np.append(
                        out_label_ids_b,
                        inputs["labels_b"].detach().cpu().numpy(),
                        axis=0,
                    )
            except Exception as ex:
                print(ex, "evaluate")
                traceback.print_stack()
        try:
            eval_loss = eval_loss / nb_eval_steps
            if args.output_mode == "classification":
                preds_a = np.argmax(preds_a, axis=1)
                preds_b = np.argmax(preds_b, axis=1)
            elif args.output_mode == "regression":
                preds_a = np.squeeze(preds_a)
                preds_b = np.squeeze(preds_b)
            if args.do_eval:
                result_a = compute_metrics(eval_task, preds_a, out_label_ids_a)
                result_b = compute_metrics(eval_task, preds_b, out_label_ids_b)
                result_a = {k + "_a": v for k, v in result_a.items()}
                result_b = {k + "_b": v for k, v in result_b.items()}
                results.update(result_a)
                results.update(result_b)
            if args.do_eval:
                output_eval_file = os.path.join(
                    eval_output_dir, prefix, "eval_results.txt"
                )
                with open(output_eval_file, "w") as writer:
                    logger.info("***** Eval results {} *****".format(prefix))
                    for key in sorted(result_a.keys()):
                        logger.info("  %s = %s", key, str(result_a[key]))
                        writer.write("%s = %s\n" % (key, str(result_a[key])))
                    for key in sorted(result_b.keys()):
                        logger.info("  %s = %s", key, str(result_b[key]))
                        writer.write("%s = %s\n" % (key, str(result_b[key])))
            output_test_predictions_file = os.path.join(
                args.output_dir, "test_predictions.txt"
            )
            with open("a_" +output_test_predictions_file, "w") as f:
                str_preds = "\n".join([str(p) for p in preds_a])
                f.write(str_preds)
            with open("b_" +output_test_predictions_file, "w") as f:
                str_preds = "\n".join([str(p) for p in preds_b])
                f.write(str_preds)
        except Exception as ex:
            traceback.print_stack()
            print("evaluation", ex)
    return results


def load_and_cache_examples(args, task, tokenizer, mode):
    if args.local_rank not in [-1, 0] and not mode in ("dev", "test"):
        torch.distributed.barrier()  # Make sure only the first process in distributed training process the dataset, and the others will use the cache

    processor = processors[task]()
    output_mode = output_modes[task]
    # Load data features from cache or dataset file
    cached_features_file = os.path.join(
        args.data_dir,
        "cached_{}_{}_{}_{}".format(
            mode,
            list(filter(None, args.model_name_or_path.split("/"))).pop(),
            str(args.max_seq_length),
            str(task),
        ),
    )
    if os.path.exists(cached_features_file) and not args.overwrite_cache:
        logger.info(
            "Loading features from cached file %s", cached_features_file
        )
        features = torch.load(cached_features_file)
    else:
        logger.info("Creating features from dataset file at %s", args.data_dir)
        label_list = processor.get_labels()
        if task in ["mnli", "mnli-mm"] and args.model_type in [
            "roberta",
            "xlmroberta",
        ]:
            # HACK(label indices are swapped in RoBERTa pretrained model)
            label_list[1], label_list[2] = label_list[2], label_list[1]
        examples = processor.get_examples(args.data_dir, mode, args.folder_list)
        features = convert_examples_to_features(
            examples,
            tokenizer,
            label_list=label_list,
            max_seq_length=args.max_seq_length,
            output_mode=output_mode,
            pad_on_left=bool(
                args.model_type in ["xlnet"]
            ),  # pad on the left for xlnet
            pad_token=tokenizer.convert_tokens_to_ids([tokenizer.pad_token])[
                0
            ],
            pad_token_segment_id=4 if args.model_type in ["xlnet"] else 0,
        )
        if args.local_rank in [-1, 0]:
            logger.info(
                "Saving features into cached file %s", cached_features_file
            )
            torch.save(features, cached_features_file)

    if args.local_rank == 0 and not evaluate:
        torch.distributed.barrier()  # Make sure only the first process in distributed training process the dataset, and the others will use the cache

    # Convert to Tensors and build dataset
    all_input_ids = torch.tensor(
        [f.input_ids for f in features], dtype=torch.long
    )
    all_attention_mask = torch.tensor(
        [f.input_mask for f in features], dtype=torch.long
    )
    all_token_type_ids = torch.tensor(
        [f.segment_ids for f in features], dtype=torch.long
    )
    if output_mode == "classification":
        all_labels_a = torch.tensor(
            [f.label_a for f in features], dtype=torch.long
        )
        all_labels_b = torch.tensor(
            [f.label_b for f in features], dtype=torch.long
        )
    elif output_mode == "regression":
        all_labels_a = torch.tensor(
            [f.label_a for f in features], dtype=torch.float
        )
        all_labels_b = torch.tensor(
            [f.label_b for f in features], dtype=torch.float
        )
    dataset = TensorDataset(
        all_input_ids,
        all_attention_mask,
        all_token_type_ids,
        all_labels_a,
        all_labels_b,
    )
    return dataset


def main():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument(
        "--data_dir",
        default=None,
        type=str,
        required=True,
        help="The input data dir. Should contain the .tsv files (or other data files) for the task.",
    )
    parser.add_argument(
        "--model_type",
        default=None,
        type=str,
        required=True,
        help="Model type selected in the list: "
        + ", ".join(MODEL_CLASSES.keys()),
    )
    parser.add_argument(
        "--model_name_or_path",
        default=None,
        type=str,
        required=True,
        help="Path to pre-trained model or shortcut name from HuggingFace",
    )
    parser.add_argument(
        "--task_name",
        default=None,
        type=str,
        required=True,
        help="The name of the task to train selected in the list: "
        + ", ".join(processors.keys()),
    )
    parser.add_argument(
        "--output_dir",
        default=None,
        type=str,
        required=True,
        help="The output directory where the model predictions and checkpoints will be written.",
    )
    parser.add_argument(
        "--folder_list",
        default=None,
        type=str,
        nargs="*",
        required=False,
        help="The folder list",
    )
    # Other parameters
    parser.add_argument(
        "--config_name",
        default="",
        type=str,
        help="Pretrained config name or path if not the same as model_name",
    )
    parser.add_argument(
        "--tokenizer_name",
        default="",
        type=str,
        help="Pretrained tokenizer name or path if not the same as model_name",
    )
    parser.add_argument(
        "--cache_dir",
        default="",
        type=str,
        help="Where do you want to store the pre-trained models downloaded from s3",
    )
    parser.add_argument(
        "--max_seq_length",
        default=128,
        type=int,
        help="The maximum total input sequence length after tokenization. Sequences longer "
        "than this will be truncated, sequences shorter will be padded.",
    )
    parser.add_argument(
        "--do_train", action="store_true", help="Whether to run training."
    )
    parser.add_argument(
        "--do_eval",
        action="store_true",
        help="Whether to run eval on the dev set.",
    )
    parser.add_argument(
        "--do_predict",
        action="store_true",
        help="Whether to run prediction on the test set.",
    )
    parser.add_argument(
        "--evaluate_during_training",
        action="store_true",
        help="Run evaluation during training at each logging step.",
    )
    parser.add_argument(
        "--do_lower_case",
        action="store_true",
        help="Set this flag if you are using an uncased model.",
    )

    parser.add_argument(
        "--per_gpu_train_batch_size",
        default=8,
        type=int,
        help="Batch size per GPU/CPU for training.",
    )
    parser.add_argument(
        "--per_gpu_eval_batch_size",
        default=8,
        type=int,
        help="Batch size per GPU/CPU for evaluation.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--learning_rate",
        default=5e-5,
        type=float,
        help="The initial learning rate for Adam.",
    )
    parser.add_argument(
        "--weight_decay",
        default=0.0,
        type=float,
        help="Weight decay if we apply some.",
    )
    parser.add_argument(
        "--adam_epsilon",
        default=1e-8,
        type=float,
        help="Epsilon for Adam optimizer.",
    )
    parser.add_argument(
        "--max_grad_norm", default=1.0, type=float, help="Max gradient norm."
    )
    parser.add_argument(
        "--num_train_epochs",
        default=3.0,
        type=float,
        help="Total number of training epochs to perform.",
    )
    parser.add_argument(
        "--max_steps",
        default=-1,
        type=int,
        help="If > 0: set total number of training steps to perform. Override num_train_epochs.",
    )
    parser.add_argument(
        "--warmup_steps",
        default=0,
        type=int,
        help="Linear warmup over warmup_steps.",
    )

    parser.add_argument(
        "--logging_steps",
        type=int,
        default=500,
        help="Log every X updates steps.",
    )
    parser.add_argument(
        "--save_steps",
        type=int,
        default=500,
        help="Save checkpoint every X updates steps.",
    )
    parser.add_argument(
        "--eval_all_checkpoints",
        action="store_true",
        help="Evaluate all checkpoints starting with the same prefix as model_name ending and ending with step number",
    )
    parser.add_argument(
        "--no_cuda",
        action="store_true",
        help="Avoid using CUDA when available",
    )
    parser.add_argument(
        "--overwrite_output_dir",
        action="store_true",
        help="Overwrite the content of the output directory",
    )
    parser.add_argument(
        "--overwrite_cache",
        action="store_true",
        help="Overwrite the cached training and evaluation sets",
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="random seed for initialization"
    )

    parser.add_argument(
        "--fp16",
        action="store_true",
        help="Whether to use 16-bit (mixed) precision (through NVIDIA apex) instead of 32-bit",
    )
    parser.add_argument(
        "--fp16_opt_level",
        type=str,
        default="O1",
        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
        "See details at https://nvidia.github.io/apex/amp.html",
    )
    parser.add_argument(
        "--local_rank",
        type=int,
        default=-1,
        help="For distributed training: local_rank",
    )
    parser.add_argument(
        "--server_ip", type=str, default="", help="For distant debugging."
    )
    parser.add_argument(
        "--server_port", type=str, default="", help="For distant debugging."
    )
    args = parser.parse_args()

    if (
        os.path.exists(args.output_dir)
        and os.listdir(args.output_dir)
        and args.do_train
        and not args.overwrite_output_dir
    ):
        raise ValueError(
            "Output directory ({}) already exists and is not empty. Use --overwrite_output_dir to overcome.".format(
                args.output_dir
            )
        )

    # Setup distant debugging if needed
    if args.server_ip and args.server_port:
        # Distant debugging - see https://code.visualstudio.com/docs/python/debugging#_attach-to-a-local-script
        import ptvsd

        print("Waiting for debugger attach")
        ptvsd.enable_attach(
            address=(args.server_ip, args.server_port), redirect_output=True
        )
        ptvsd.wait_for_attach()

    # Setup CUDA, GPU & distributed training
    if args.local_rank == -1 or args.no_cuda:
        device = torch.device(
            "cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu"
        )
        args.n_gpu = 0 if args.no_cuda else torch.cuda.device_count()
    else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend="nccl")
        args.n_gpu = 1
    args.device = device

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN,
    )
    logger.warning(
        "Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
        args.local_rank,
        device,
        args.n_gpu,
        bool(args.local_rank != -1),
        args.fp16,
    )

    # Set seed
    set_seed(args)

    # Prepare GLUE task
    args.task_name = args.task_name.lower()
    if args.task_name not in processors:
        raise ValueError("Task not found: %s" % (args.task_name))
    processor = processors[args.task_name]()
    args.output_mode = output_modes[args.task_name]
    label_list = processor.get_labels()
    num_labels_a = len(label_list["a"])
    num_labels_b = len(label_list["b"])

    # Load pretrained model and tokenizer
    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

    args.model_type = args.model_type.lower()
    config_class, model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    config = config_class.from_pretrained(
        args.config_name if args.config_name else args.model_name_or_path,
        finetuning_task=args.task_name,
        cache_dir=args.cache_dir if args.cache_dir else None,
    )
    config.num_labels_a = num_labels_a
    config.num_labels_b = num_labels_b
    tokenizer = tokenizer_class.from_pretrained(
        args.tokenizer_name
        if args.tokenizer_name
        else args.model_name_or_path,
        do_lower_case=args.do_lower_case,
        cache_dir=args.cache_dir if args.cache_dir else None,
    )
    model = MultiHeadClassification.from_pretrained(
        args.model_name_or_path,
        from_tf=bool(".ckpt" in args.model_name_or_path),
        config=config,
        cache_dir=args.cache_dir if args.cache_dir else None,
    )

    if args.local_rank == 0:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

    model.to(args.device)

    logger.info("Training/evaluation parameters %s", args)

    # Training
    if args.do_train:
        train_dataset = load_and_cache_examples(
            args, args.task_name, tokenizer, "train"
        )
        global_step, tr_loss = train(args, train_dataset, model, tokenizer)
        logger.info(
            " global_step = %s, average loss = %s", global_step, tr_loss
        )

    # Saving best-practices: if you use defaults names for the model, you can reload it using from_pretrained()
    if args.do_train and (
        args.local_rank == -1 or torch.distributed.get_rank() == 0
    ):
        # Create output directory if needed
        if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
            os.makedirs(args.output_dir)

        logger.info("Saving model checkpoint to %s", args.output_dir)
        # Save a trained model, configuration and tokenizer using `save_pretrained()`.
        # They can then be reloaded using `from_pretrained()`
        model_to_save = (
            model.module if hasattr(model, "module") else model
        )  # Take care of distributed/parallel training
        model_to_save.save_pretrained(args.output_dir)
        tokenizer.save_pretrained(args.output_dir)

        # Good practice: save your training arguments together with the trained model
        torch.save(args, os.path.join(args.output_dir, "training_args.bin"))

        # Load a trained model and vocabulary that you have fine-tuned
        model = MultiHeadClassification.from_pretrained(args.output_dir)
        tokenizer = tokenizer_class.from_pretrained(args.output_dir)
        model.to(args.device)

    # Evaluation
    results = {}
    if (args.do_predict or args.do_eval) and args.local_rank in [-1, 0]:
        tokenizer = tokenizer_class.from_pretrained(
            args.output_dir, do_lower_case=args.do_lower_case
        )
        checkpoints = [args.output_dir]
        if args.eval_all_checkpoints:
            checkpoints = list(
                os.path.dirname(c)
                for c in sorted(
                    glob.glob(
                        args.output_dir + "/**/" + WEIGHTS_NAME, recursive=True
                    )
                )
            )
            logging.getLogger("transformers.modeling_utils").setLevel(
                logging.WARN
            )  # Reduce logging
        logger.info("Evaluate the following checkpoints: %s", checkpoints)
        for checkpoint in checkpoints:
            global_step = (
                checkpoint.split("-")[-1] if len(checkpoints) > 1 else ""
            )
            prefix = (
                checkpoint.split("/")[-1]
                if checkpoint.find("checkpoint") != -1
                else ""
            )

            model = MultiHeadClassification.from_pretrained(checkpoint)
            model.to(args.device)
            try:
                result = evaluate(args, model, tokenizer, prefix=prefix)
                if args.do_eval:
                    result = dict(
                        (k + "_{}".format(global_step), v)
                        for k, v in result.items()
                    )
                    results.update(result)
            except Exception as ex:
                print(ex, "main-evaluate")
                traceback.print_stack()
    logger.info("trained")
    return results


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        print(ex, "main")
        traceback.print_stack()
