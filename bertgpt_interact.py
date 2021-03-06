import os
import sys
import logging
import random
from argparse import ArgumentParser
from itertools import chain
from pprint import pformat
import warnings
from datetime import datetime
from tqdm import tqdm

import torch
import torch.nn.functional as F

from transformers import OpenAIGPTLMHeadModel, GPT2Config, OpenAIGPTConfig, OpenAIGPTTokenizer, GPT2LMHeadModel, GPT2Tokenizer, BertTokenizer, BertModel
from bertgpt_train import SPECIAL_TOKENS, build_input_from_segments, add_special_tokens_
from bertgpt_utils import get_test_datasetEN2, download_pretrained_model

def top_filtering(logits, top_k=0., top_p=0.9, threshold=-float('Inf'), filter_value=-float('Inf')):
    assert logits.dim() == 1  # Only work for batch size 1 for now - could update but it would obfuscate a bit the code
    top_k = min(top_k, logits.size(-1))
    if top_k > 0:
        # Remove all tokens with a probability less than the last token in the top-k tokens
        indices_to_remove = logits < torch.topk(logits, top_k)[0][..., -1, None]
        logits[indices_to_remove] = filter_value

    if top_p > 0.0:
        # Compute cumulative probabilities of sorted tokens
        sorted_logits, sorted_indices = torch.sort(logits, descending=True)
        cumulative_probabilities = torch.cumsum(F.softmax(sorted_logits, dim=-1), dim=-1)

        # Remove tokens with cumulative probability above the threshold
        sorted_indices_to_remove = cumulative_probabilities > top_p
        # Shift the indices to the right to keep also the first token above the threshold
        sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
        sorted_indices_to_remove[..., 0] = 0

        # Back to unsorted indices and set them to -infinity
        indices_to_remove = sorted_indices[sorted_indices_to_remove]
        logits[indices_to_remove] = filter_value

    indices_to_remove = logits < threshold
    logits[indices_to_remove] = filter_value

    return logits


def sample_sequence(source, bert_tokenizer, model, bert_model, gpt_tokenizer, args, current_output=None):
    special_tokens_ids = gpt_tokenizer.convert_tokens_to_ids(SPECIAL_TOKENS)
    if current_output is None:
        current_output = []

    for i in range(args.max_length):
        instance = build_input_from_segments(source, current_output, bert_tokenizer, gpt_tokenizer, with_eos=False)

        input_ids = torch.tensor(instance["source_ids"], device=args.device).unsqueeze(0)
        target_ids = torch.tensor(instance["target_ids"], device=args.device).unsqueeze(0)

        encoded_layers, _ = bert_model(input_ids)
        logits = model(target_ids, encoded_layers)
        if isinstance(logits, tuple):  # for gpt2 and maybe others
            logits = logits[0]
        logits = logits[0, -1, :] / args.temperature
        logits = top_filtering(logits, top_k=args.top_k, top_p=args.top_p)
        probs = F.softmax(logits, dim=-1)

        prev = torch.topk(probs, 1)[1] if args.no_sample else torch.multinomial(probs, 1)
        if i < args.min_length and prev.item() in special_tokens_ids:
            while prev.item() in special_tokens_ids:
                if probs.max().item() == 1:
                    warnings.warn("Warning: model generating special token with probability 1.")
                    break  # avoid infinitely looping over special token
                prev = torch.multinomial(probs, num_samples=1)

        if prev.item() in special_tokens_ids:
            break
        current_output.append(prev.item())

    return current_output

def run():
    parser = ArgumentParser()
    parser.add_argument("--dataset_path", type=str, default="", help="Path or url of the dataset.")
    parser.add_argument("--model_checkpoint", type=str, default="", help="Path, url or short name of the model")
    parser.add_argument("--device", type=str, default="cuda" if torch.cuda.is_available() else "cpu", help="Device (cuda or cpu)")
    parser.add_argument("--gpt2_model_name", type=str, default="gpt2", help="name of the model ex)openai-gpt")
    parser.add_argument("--no_sample", action='store_true', help="Set to use greedy decoding instead of sampling")
    parser.add_argument("--max_length", type=int, default=30, help="Maximum length of the output utterances")
    parser.add_argument("--min_length", type=int, default=4, help="Minimum length of the output utterances")
    parser.add_argument("--seed", type=int, default=0, help="Seed")
    parser.add_argument("--temperature", type=int, default=0.8, help="Sampling softmax temperature")
    parser.add_argument("--top_k", type=int, default=8, help="Filter top-k tokens before sampling (<=0: no filtering)")
    parser.add_argument("--top_p", type=float, default=0.9, help="Nucleus filtering (top-p) before sampling (<=0.0: no filtering)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__file__)
    logger.info(pformat(args))

    if args.seed != 0:
        random.seed(args.seed)
        torch.random.manual_seed(args.seed)
        torch.cuda.manual_seed(args.seed)


    logger.info("Get pretrained model and tokenizer")
    bert_tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
    bert_model = BertModel.from_pretrained('bert-base-uncased')
    bert_model.to(args.device)
    bert_model.eval()

    tokenizer_class = GPT2Tokenizer if "gpt2" in args.gpt2_model_name else OpenAIGPTTokenizer  # cant use Autotokenizer because checkpoint could be a Path
    tokenizer = tokenizer_class.from_pretrained(args.model_checkpoint)

    config_class = GPT2Config if "gpt2" in args.gpt2_model_name else OpenAIGPTConfig
    gpt_config = config_class.from_pretrained(args.model_checkpoint)
    gpt_config.adapter = False
    gpt_config.keyword_module = ""

    model_class = GPT2LMHeadModel if "gpt2" in args.gpt2_model_name else OpenAIGPTLMHeadModel
    model = model_class.from_pretrained(args.model_checkpoint, config=gpt_config)
    model.to(args.device)
    add_special_tokens_(model, tokenizer)
    model.eval()

    sourceList, targetList = get_test_datasetEN2(bert_tokenizer, tokenizer, args.dataset_path)
    current_time = datetime.now().strftime('%b%d_%H-%M-%S')
    f1 = open((args.model_checkpoint + current_time + "_output.txt"), 'w')
    for line in tqdm(zip(sourceList, targetList), total=len(sourceList)):
        out_ids = sample_sequence(line[0], bert_tokenizer, model, bert_model, tokenizer, args)
        out_texts = tokenizer.decode(out_ids)
        for text in out_texts:
            f1.write(text.replace('???', ' ').replace('</s>', ' '))
        f1.write("\n")
    f1.close()


if __name__ == "__main__":
    run()
