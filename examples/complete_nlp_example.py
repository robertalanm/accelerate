# coding=utf-8
# Copyright 2021 The HuggingFace Inc. team. All rights reserved.
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
import argparse
import os
import bittensor as bt
from bittensor._neuron.text.core_server.nucleus_impl import server

import pdb
import torch
from torch.optim import AdamW
from torch.utils.data import DataLoader

from typing import Sequence

import evaluate
from accelerate import Accelerator, DistributedType
from datasets import load_dataset, interleave_datasets
from transformers import AutoModelForCausalLM, get_linear_schedule_with_warmup, set_seed, GPTNeoForCausalLM



########################################################################
# This is a fully working simple example to use Accelerate
#
# This example trains a Bert base model on GLUE MRPC
# in any of the following settings (with the same script):
#   - single CPU or single GPU
#   - multi GPUS (using PyTorch distributed mode)
#   - (multi) TPUs
#   - fp16 (mixed-precision) or fp32 (normal precision)
#
# This example also demonstrates the checkpointing and sharding capabilities
#
# To run it in each of these various modes, follow the instructions
# in the readme for examples:
# https://github.com/huggingface/accelerate/tree/main/examples
#
########################################################################


MAX_GPU_BATCH_SIZE = 2
EVAL_BATCH_SIZE = 32


def create_tokenizer():
    return bt.tokenizer()

def create_genesis_dataset(config, seq_len: int):
    # find all the text files inside the directory ~/genesis
    # and create a list of their paths
    dataset_path = os.path.expanduser('~') + '/genesis'
    file_paths = []
    for root, dirs, files in os.walk(dataset_path):
        for file in files:
            if file.endswith(".txt"):
                file_paths.append(os.path.join(root, file))

    # interleave the datasets
    datasets_list = []
    datasets_args = {}
    datasets_args["keep_linebreaks"] = True
    for file_path in file_paths:
        data = load_dataset("text", data_files=file_path, **datasets_args)['train']
        # pdb.set_trace()
        datasets_list.append(data)
    # pdb.set_trace()
    dataset = interleave_datasets(datasets_list)

    # convert to torch tensors
    # dataset = dataset.with_format("torch")

    # tokenize the dataset
    tokenizer = create_tokenizer()
    def encode(examples):
        return tokenizer(
            examples["text"], truncation=True, max_length=seq_len, padding="max_length")
    
    pre_dataset = dataset.map(encode, batched=False, remove_columns=["text"])

    # shuffle the dataset and split it into train and validation with 75% and 25% respectively
    seed, buffer_size = 12976371472801, 10_000
    pre_dataset = pre_dataset.shuffle(seed)
    train_dataset = pre_dataset.select(range(int(len(pre_dataset) * 0.75)))
    val_dataset = pre_dataset.select(range(int(len(pre_dataset) * 0.75), len(pre_dataset)))

    return train_dataset, val_dataset, tokenizer




def training_function(config, args):
    # Initialize accelerator
    if args.with_tracking:
        accelerator = Accelerator(
            cpu=args.cpu, mixed_precision=args.mixed_precision, log_with="all", logging_dir=args.logging_dir
        )
    else:
        accelerator = Accelerator(cpu=args.cpu, mixed_precision=args.mixed_precision)

    if hasattr(args.checkpointing_steps, "isdigit"):
        if args.checkpointing_steps == "epoch":
            checkpointing_steps = args.checkpointing_steps
        elif args.checkpointing_steps.isdigit():
            checkpointing_steps = int(args.checkpointing_steps)
        else:
            raise ValueError(
                f"Argument `checkpointing_steps` must be either a number or `epoch`. `{args.checkpointing_steps}` passed."
            )
    else:
        checkpointing_steps = None
    # Sample hyper-parameters for learning rate, batch size, seed and a few other HPs
    lr = config["lr"]
    num_epochs = int(config["num_epochs"])
    seed = int(config["seed"])
    batch_size = int(config["batch_size"])
    n_positions = 2048

    # We need to initialize the trackers we use, and also store our configuration
    if args.with_tracking:
        run = os.path.split(__file__)[-1].split(".")[0]
        accelerator.init_trackers(run, config)

    # Apply the method we just defined to all the examples in all the splits of the dataset
    # starting with the main process first:
    with accelerator.main_process_first():
        # Initialize the tokenizer and model
        # TODO: Config
        train_dataset, val_dataset, tokenizer = create_genesis_dataset(config, n_positions)  

    # We also rename the 'label' column to 'labels' which is the expected name for labels by the models of the
    # transformers library
    # tokenized_datasets = tokenized_datasets.rename_column("label", "labels")

    # If the batch size is too big we use gradient accumulation
    gradient_accumulation_steps = 1
    if batch_size > MAX_GPU_BATCH_SIZE and accelerator.distributed_type != DistributedType.TPU:
        gradient_accumulation_steps = batch_size // MAX_GPU_BATCH_SIZE
        batch_size = MAX_GPU_BATCH_SIZE

    def collate_fn(examples):
        # On TPU it's best to pad everything to the same length or training will be very slow.
        if accelerator.distributed_type == DistributedType.TPU:
            return tokenizer.pad(examples, padding="max_length", max_length=128, return_tensors="pt")
        return tokenizer.pad(examples, padding="longest", return_tensors="pt")

    # Instantiate dataloaders.
    train_dataloader = DataLoader(
        train_dataset, shuffle=True, collate_fn=collate_fn, batch_size=batch_size
    )
    eval_dataloader = DataLoader(
        val_dataset, shuffle=False, collate_fn=collate_fn, batch_size=EVAL_BATCH_SIZE
    )

    set_seed(seed)
    server_config = server.config()
    server_config.model_name = "EleutherAI/gpt-neo-125M"
    # Instantiate the model (we build the model here so that the seed also control new weights initialization)
    # model = GPTNeoForCausalLM.from_pretrained("EleutherAI/gpt-neo-125M")
    model = server(config=server_config)
    # glue_metric = evaluate.load('glue', 'sst2')
    frugalscore = evaluate.load("frugalscore", "moussaKam/frugalscore_medium_bert-base_mover-score")
    # We could avoid this line since the accelerator is set with `device_placement=True` (default value).
    # Note that if you are placing tensors on devices manually, this line absolutely needs to be before the optimizer
    # creation otherwise training will not work on TPU (`accelerate` will kindly throw an error to make us aware of that).
    model = model.to(accelerator.device)

    # Instantiate optimizer
    optimizer = AdamW(params=model.parameters(), lr=lr)

    # Instantiate scheduler
    lr_scheduler = get_linear_schedule_with_warmup(
        optimizer=optimizer,
        num_warmup_steps=100,
        num_training_steps=(len(train_dataloader) * num_epochs) // gradient_accumulation_steps,
    )

    # Prepare everything
    # There is no specific order to remember, we just need to unpack the objects in the same order we gave them to the
    # prepare method.
    model, optimizer, train_dataloader, eval_dataloader, lr_scheduler = accelerator.prepare(
        model, optimizer, train_dataloader, eval_dataloader, lr_scheduler
    )

    # We need to keep track of how many total steps we have iterated over
    overall_step = 0
    # We also need to keep track of the stating epoch so files are named properly
    starting_epoch = 0

    # Potentially load in the weights and states from a previous save
    if args.resume_from_checkpoint:
        if args.resume_from_checkpoint is not None or args.resume_from_checkpoint != "":
            accelerator.print(f"Resumed from checkpoint: {args.resume_from_checkpoint}")
            accelerator.load_state(args.resume_from_checkpoint)
            path = os.path.basename(args.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = [f.name for f in os.scandir(os.getcwd()) if f.is_dir()]
            dirs.sort(key=os.path.getctime)
            path = dirs[-1]  # Sorts folders by date modified, most recent checkpoint is the last
        # Extract `epoch_{i}` or `step_{i}`
        training_difference = os.path.splitext(path)[0]

        if "epoch" in training_difference:
            starting_epoch = int(training_difference.replace("epoch_", "")) + 1
            resume_step = None
        else:
            resume_step = int(training_difference.replace("step_", ""))
            starting_epoch = resume_step // len(train_dataloader)
            resume_step -= starting_epoch * len(train_dataloader)

    # Now we train the model
    for epoch in range(starting_epoch, num_epochs):
        model.train()
        if args.with_tracking:
            total_loss = 0
        for step, batch in enumerate(train_dataloader):
            # We need to skip steps until we reach the resumed step
            if args.resume_from_checkpoint and epoch == starting_epoch:
                if resume_step is not None and step < resume_step:
                    overall_step += 1
                    continue
            # We could avoid this line since we set the accelerator with `device_placement=True`.
            batch.to(accelerator.device)
            outputs = model(**batch)
            if accelerator.is_main_process:
                pdb.set_trace()
            loss = outputs.loss
            loss = loss / gradient_accumulation_steps
            # We keep track of the loss at each epoch
            if args.with_tracking:
                total_loss += loss.detach().float()
            accelerator.backward(loss)
            if step % gradient_accumulation_steps == 0:
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()

            overall_step += 1

            if isinstance(checkpointing_steps, int):
                output_dir = f"step_{overall_step}"
                if overall_step % checkpointing_steps == 0:
                    if args.output_dir is not None:
                        output_dir = os.path.join(args.output_dir, output_dir)
                    accelerator.save_state(output_dir)

        model.eval()
        for step, batch in enumerate(eval_dataloader):
            # We could avoid this line since we set the accelerator with `device_placement=True`.
            batch.to(accelerator.device)
            with torch.no_grad():
                outputs = model(**batch)
            predictions = outputs.logits.argmax(dim=-1)
            predictions, references = accelerator.gather_for_metrics((predictions, batch["labels"]))
            # glue_metric.add_batch(
            #     predictions=predictions,
            #     references=references,
            # )
            frugalscore.add_batch(
                predictions=predictions,
                references=references,
            )

            
        # glue_results = glue_metric.compute()
        frugalscore_results = frugalscore.compute()
        # Use accelerator.print to print only on the main process.
        # accelerator.print(f"epoch {epoch}:", glue_results)
        accelerator.print(f"epoch {epoch}:", frugalscore_results)
        if args.with_tracking:
            # accelerator.log(
            #     {
            #         "name": "glue",
            #         "accuracy": glue_results["accuracy"],
            #         "f1": glue_results["f1"],
            #         "train_loss": total_loss.item() / len(train_dataloader),
            #         "epoch": epoch,
            #     },
            #     step=epoch,
            # )

            accelerator.log(
                {
                    "name": "frugalscore",
                    "scores": frugalscore_results["scores"],
                    "train_loss": total_loss.item() / len(train_dataloader),
                    "epoch": epoch,
                },
                step=epoch,
            )

        if checkpointing_steps == "epoch":
            output_dir = f"epoch_{epoch}"
            if args.output_dir is not None:
                output_dir = os.path.join(args.output_dir, output_dir)
            accelerator.save_state(output_dir)

    if args.with_tracking:
        accelerator.end_training()


def main():
    parser = argparse.ArgumentParser(description="Simple example of training script.")
    parser.add_argument(
        "--mixed_precision",
        type=str,
        default="no",
        choices=["no", "fp16", "bf16"],
        help="Whether to use mixed precision. Choose"
        "between fp16 and bf16 (bfloat16). Bf16 requires PyTorch >= 1.10."
        "and an Nvidia Ampere GPU.",
    )
    parser.add_argument("--cpu", action="store_true", help="If passed, will train on the CPU.")
    parser.add_argument(
        "--checkpointing_steps",
        type=str,
        default=None,
        help="Whether the various states should be saved at the end of every n steps, or 'epoch' for each epoch.",
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help="If the training should continue from a checkpoint folder.",
    )
    parser.add_argument(
        "--with_tracking",
        action="store_true",
        help="Whether to load in all available experiment trackers from the environment and use them for logging.",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default=".",
        help="Optional save directory where all checkpoint folders will be stored. Default is the current working directory.",
    )
    parser.add_argument(
        "--logging_dir",
        type=str,
        default="logs",
        help="Location on where to store experiment tracking logs`",
    )
    args = parser.parse_args()
    config = {"lr": 2e-5, "num_epochs": 3, "seed": 42, "batch_size": 16}
    # bt_config = bt.config(parser=args)
    training_function(config, args)


if __name__ == "__main__":
    main()
