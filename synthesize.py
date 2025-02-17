import re
import argparse
from string import punctuation
from pathlib import Path
from tqdm import tqdm

import torch
import yaml
import numpy as np
from torch.utils.data import DataLoader
from g2p_en import G2p
from pypinyin import pinyin, Style

from utils.model import get_model, get_vocoder
from utils.tools import to_device, synth_samples
from dataset import TextDataset, DatasetForTest
from text import text_to_sequence, symbols
import pyopenjtalk
from prepare_tg_accent import pp_symbols
from convert_label import openjtalk2julius

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def read_lexicon(lex_path):
    lexicon = {}
    with open(lex_path) as f:
        for line in f:
            temp = re.split(r"\s+", line.strip("\n"))
            word = temp[0]
            phones = temp[1:]
            if word.lower() not in lexicon:
                lexicon[word.lower()] = phones
    return lexicon


def preprocess_english(text, preprocess_config):
    text = text.rstrip(punctuation)
    lexicon = read_lexicon(preprocess_config["path"]["lexicon_path"])

    g2p = G2p()
    phones = []
    words = re.split(r"([,;.\-\?\!\s+])", text)
    for w in words:
        if w.lower() in lexicon:
            phones += lexicon[w.lower()]
        else:
            phones += list(filter(lambda p: p != " ", g2p(w)))
    phones = "{" + "}{".join(phones) + "}"
    phones = re.sub(r"\{[^\w\s]?\}", "{sp}", phones)
    phones = phones.replace("}{", " ")

    print("Raw Text Sequence: {}".format(text))
    print("Phoneme Sequence: {}".format(phones))
    sequence = np.array(
        text_to_sequence(
            phones, preprocess_config["preprocessing"]["text"]["text_cleaners"]
        )
    )

    return np.array(sequence)


def preprocess_mandarin(text, preprocess_config):
    lexicon = read_lexicon(preprocess_config["path"]["lexicon_path"])

    phones = []
    pinyins = [
        p[0]
        for p in pinyin(
            text, style=Style.TONE3, strict=False, neutral_tone_with_five=True
        )
    ]
    for p in pinyins:
        if p in lexicon:
            phones += lexicon[p]
        else:
            phones.append("sp")

    phones = "{" + " ".join(phones) + "}"
    print("Raw Text Sequence: {}".format(text))
    print("Phoneme Sequence: {}".format(phones))
    sequence = np.array(
        text_to_sequence(
            phones, preprocess_config["preprocessing"]["text"]["text_cleaners"]
        )
    )

    return np.array(sequence)

def preprocess_japanese(text:str):
    fullcontext_labels = pyopenjtalk.extract_fullcontext(text)
    phonemes , accents = pp_symbols(fullcontext_labels)
    phonemes = [openjtalk2julius(p) for p in phonemes if p != '']
    return phonemes, accents



def synthesize(model, step, configs, vocoder, batchs, control_values,
               result_path):
    preprocess_config, model_config, train_config = configs
    pitch_control, energy_control, duration_control = control_values

    result_path = result_path / "data"
    result_path.mkdir(parents=True, exist_ok=True)

    use_fp_tag = train_config["use_fp_tag"]

    use_accent = preprocess_config["preprocessing"]["accent"]["use_accent"]

    for batch in tqdm(batchs):
        batch = to_device(batch, device, use_accent=use_accent, use_fp_tag=use_fp_tag)
        accents = None
        fp_tag = None
        if use_fp_tag:
            if use_accent:
                accents = batch[-2]
                fp_tag = batch[-1]
                batch = batch[:-2]
            else:
                fp_tag = batch[-1]
                batch = batch[:-1]
        else:
            if use_accent:
                accents = batch[-1]
                batch = batch[:-1]

        with torch.no_grad():
            # Forward
            output = model(
                *(batch[2:]),
                p_control=pitch_control,
                e_control=energy_control,
                d_control=duration_control,
                accents=accents,
                fp_tag=fp_tag
            )
            synth_samples(
                batch,
                output,
                vocoder,
                model_config,
                preprocess_config,
                result_path,
            )


if __name__ == "__main__":

    # parser = argparse.ArgumentParser()
    # parser.add_argument("--restore_step", type=int, required=True)
    # parser.add_argument(
    #     "--mode",
    #     type=str,
    #     choices=["batch", "single"],
    #     required=True,
    #     help="Synthesize a whole dataset or a single sentence",
    # )
    # parser.add_argument(
    #     "--source",
    #     type=str,
    #     default=None,
    #     help="path to a source file with format like train.txt and val.txt, for batch mode only",
    # )
    # parser.add_argument(
    #     "--text",
    #     type=str,
    #     default=None,
    #     help="raw text to synthesize, for single-sentence mode only",
    # )
    # parser.add_argument(
    #     "--speaker_id",
    #     type=int,
    #     default=0,
    #     help="speaker ID for multi-speaker synthesis, for single-sentence mode only",
    # )
    # parser.add_argument(
    #     "-p",
    #     "--preprocess_config",
    #     type=str,
    #     required=True,
    #     help="path to preprocess.yaml",
    # )
    # parser.add_argument(
    #     "-m", "--model_config", type=str, required=True, help="path to model.yaml"
    # )
    # parser.add_argument(
    #     "-t", "--train_config", type=str, required=True, help="path to train.yaml"
    # )
    # parser.add_argument(
    #     "--pitch_control",
    #     type=float,
    #     default=1.0,
    #     help="control the pitch of the whole utterance, larger value for higher pitch",
    # )
    # parser.add_argument(
    #     "--energy_control",
    #     type=float,
    #     default=1.0,
    #     help="control the energy of the whole utterance, larger value for larger volume",
    # )
    # parser.add_argument(
    #     "--duration_control",
    #     type=float,
    #     default=1.0,
    #     help="control the speed of the whole utterance, larger value for slower speaking rate",
    # )
    # args = parser.parse_args()

    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=str, help="path to synthesize.yaml")
    parser.add_argument("--restore_step", type=int, default=0, help="restore step")
    args = parser.parse_args()

    config = yaml.load(open(args.config, "r"), Loader=yaml.FullLoader)
    config["restore_step"] = args.restore_step

    # Check source texts
    if config["mode"] == "batch":
        assert config["source"] is not None and config["text"] is None
    if config["mode"] == "single":
        assert config["source"] is None and config["text"] is not None

    # Read Config
    preprocess_config = yaml.load(
        open(config["preprocess_config"], "r"), Loader=yaml.FullLoader
    )
    model_config = yaml.load(open(config["model_config"], "r"), Loader=yaml.FullLoader)
    train_config = yaml.load(open(config["train_config"], "r"), Loader=yaml.FullLoader)
    configs = (preprocess_config, model_config, train_config)

    result_path = Path(config["result_path"])
    result_path.mkdir(parents=True, exist_ok=True)
    with open(result_path / "synthesize.yaml", "w") as f:
        yaml.dump(config, f)
    with open(result_path / "preprocess.yaml", "w") as f:
        yaml.dump(preprocess_config, f)
    with open(result_path / "model.yaml", "w") as f:
        yaml.dump(model_config, f)
    with open(result_path / "train.yaml", "w") as f:
        yaml.dump(train_config, f)

    # Get model
    model = get_model(args.restore_step, configs, device, train=False)

    # Load vocoder
    vocoder = get_vocoder(model_config, device)

    # Preprocess texts
    if config["mode"] == "batch":
        # Get dataset
        if config["target"]:
            dataset = DatasetForTest(
                config["source"].split("/")[-1], preprocess_config, train_config,
            )
            dataloader = DataLoader(
                dataset,
                batch_size=32,
                collate_fn=dataset.collate_fn,
            )
            control_values = config["pitch_control"], config["energy_control"], config["duration_control"]
            synthesize(model, config["restore_step"], configs, vocoder,
                       dataloader, control_values, result_path)
        else:
            dataset = TextDataset(config["source"], preprocess_config, train_config)
            dataloader = DataLoader(
                dataset,
                batch_size=8,
                collate_fn=dataset.collate_fn,
            )
            control_values = config["pitch_control"], config["energy_control"], config["duration_control"]
            synthesize(model, config["restore_step"], configs, vocoder,
                       dataloader, control_values, result_path)

    symbol_to_id = {s: i for i, s in enumerate(symbols)}
    accent_to_id = {'0':0, '[':1, ']':2, '#':3}

    if config["mode"] == "single":
        ids = raw_texts = [config["text"][:100]]
        speakers = np.array([config["speaker_id"]])
        if preprocess_config["preprocessing"]["text"]["language"] == "en":
            texts = np.array([preprocess_english(config["text"], preprocess_config)])
        elif preprocess_config["preprocessing"]["text"]["language"] == "zh":
            texts = np.array([preprocess_mandarin(config["text"], preprocess_config)])
        elif preprocess_config["preprocessing"]["text"]["language"] == "ja":
            texts = np.array([[symbol_to_id[t] for t in config["text"].replace("{", "").replace("}", "").split()]])
            phonemes, accents = preprocess_japanese(config["text"])
            print(phonemes,accents)
            texts = np.array([[symbol_to_id[t] for t in phonemes]])
            if preprocess_config["preprocessing"]["accent"]["use_accent"]:
                accents = np.array([[accent_to_id[a] for a in accents]])
            else:
                accents = None

        text_lens = np.array([len(texts[0])])
        print(text_lens)
        batchs = [(ids, raw_texts, speakers, texts, text_lens, max(text_lens), accents)]

        control_values = config["pitch_control"], config["energy_control"], \
                         config["duration_control"]
        synthesize(model, config["restore_step"], configs, vocoder, batchs,
                   control_values, result_path)