import gc
import glob
import hashlib
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from os.path import join as pjoin

import torch
from bs4 import BeautifulSoup as bs
from multiprocess import Pool

from others.log import logger
from others.tokenization import BertTokenizer
from others.utils import clean
from prepro.utils import _get_word_ngrams

nyt_remove_words = ["photo", "graph", "chart", "map", "table", "drawing"]


def recover_from_corenlp(s):
    s = re.sub(r' \'{\w}', '\'\g<1>', s)
    s = re.sub(r'\'\' {\w}', '\'\'\g<1>', s)


def load_pdf_ppt_jsons(pdf_ppt_jsons, lower=True, extractive=True):
    pdf_json, ppt_json = pdf_ppt_jsons
    source = []
    tgt = []
    for sent in json.load(open(pdf_json))['sentences']:
        tokens = [t['word'] for t in sent['tokens']]
        if lower:
            tokens = [t.lower() for t in tokens]
        source.append(tokens)

    for sent in json.load(open(ppt_json))['sentences']:
        tokens = [t['word'] for t in sent['tokens']]
        if lower:
            tokens = [t.lower() for t in tokens]
        tgt.append(tokens)

    source = [clean(' '.join(sent)).split() for sent in source]
    tgt = [clean(' '.join(sent)).split() for sent in tgt]
    return {'src': source, 'tgt': tgt}


def load_json(p, lower):
    source = []
    tgt = []
    sentence_sections = []
    flag = False
    section = 0
    for sent in json.load(open(p))['sentences']:
        tokens = [t['word'] for t in sent['tokens']]
        tokens_after = [t['after'] for t in sent['tokens']]

        if lower:
            tokens = [t.lower() for t in tokens]

        if tokens[0] == '@highlight':
            flag = True
            tgt.append([])
            continue
        if flag:
            tgt[-1].extend(tokens)
        else:
            source.append(tokens)
            sentence_sections.append(section)
            if tokens_after[-1] == '\n':
                section += 1

    source = [clean(' '.join(sent)).split() for sent in source]
    tgt = [clean(' '.join(sent)).split() for sent in tgt]
    return source, sentence_sections, tgt


def load_xml(p):
    tree = ET.parse(p)
    root = tree.getroot()
    title, byline, abs, paras = [], [], [], []
    title_node = list(root.iter('hedline'))
    if (len(title_node) > 0):
        try:
            title = [p.text.lower().split() for p in list(title_node[0].iter('hl1'))][0]
        except:
            print(p)

    else:
        return None, None
    byline_node = list(root.iter('byline'))
    byline_node = [n for n in byline_node if n.attrib['class'] == 'normalized_byline']
    if len(byline_node) > 0:
        byline = byline_node[0].text.lower().split()
    abs_node = list(root.iter('abstract'))
    if len(abs_node) > 0:
        try:
            abs = [p.text.lower().split() for p in list(abs_node[0].iter('p'))][0]
        except:
            print(p)

    else:
        return None, None
    abs = ' '.join(abs).split(';')
    abs[-1] = abs[-1].replace('(m)', '')
    abs[-1] = abs[-1].replace('(s)', '')

    for ww in nyt_remove_words:
        abs[-1] = abs[-1].replace('(' + ww + ')', '')
    abs = [p.split() for p in abs]
    abs = [p for p in abs if len(p) > 2]

    for doc_node in root.iter('block'):
        att = doc_node.get('class')
        # if(att == 'abstract'):
        #     abs = [p.text for p in list(f.iter('p'))]
        if att == 'full_text':
            paras = [p.text.lower().split() for p in list(doc_node.iter('p'))]
            break
    if len(paras) > 0:
        if len(byline) > 0:
            paras = [title + ['[unused3]'] + byline + ['[unused4]']] + paras
        else:
            paras = [title + ['[unused3]']] + paras

        return paras, abs
    else:
        return None, None


def tokenize(args):
    stories_dir = os.path.abspath(args.raw_path)
    tokenized_stories_dir = os.path.abspath(args.save_path)

    print("Preparing to tokenize %s to %s..." % (stories_dir, tokenized_stories_dir))
    stories = os.listdir(stories_dir)
    # make IO list file
    print("Making list of files to tokenize...")
    with open("mapping_for_corenlp.txt", "w") as f:
        for s in stories:
            if not s.endswith('story'):
                continue
            f.write("%s\n" % (os.path.join(stories_dir, s)))
    command = ['java', 'edu.stanford.nlp.pipeline.StanfordCoreNLP', '-annotators', 'tokenize,ssplit',
               '-ssplit.newlineIsSentenceBreak', 'always', '-filelist', 'mapping_for_corenlp.txt', '-outputFormat',
               'json', '-outputDirectory', tokenized_stories_dir]
    print("Tokenizing %i files in %s and saving in %s..." % (len(stories), stories_dir, tokenized_stories_dir))
    subprocess.call(command)
    print("Stanford CoreNLP Tokenizer has finished.")
    os.remove("mapping_for_corenlp.txt")

    # Check that the tokenized stories directory contains the same number of files as the original directory
    num_orig = len(os.listdir(stories_dir))
    num_tokenized = len(os.listdir(tokenized_stories_dir))
    if num_orig != num_tokenized:
        raise Exception(
            "The tokenized stories directory %s contains %i files, but it should contain the same number as %s (which has %i files). Was there an error during tokenization?" % (
                tokenized_stories_dir, num_tokenized, stories_dir, num_orig))
    print("Successfully finished tokenizing %s to %s.\n" % (stories_dir, tokenized_stories_dir))


def cal_rouge(evaluated_ngrams, reference_ngrams):
    reference_count = len(reference_ngrams)
    evaluated_count = len(evaluated_ngrams)

    overlapping_ngrams = evaluated_ngrams.intersection(reference_ngrams)
    overlapping_count = len(overlapping_ngrams)

    if evaluated_count == 0:
        precision = 0.0
    else:
        precision = overlapping_count / evaluated_count

    if reference_count == 0:
        recall = 0.0
    else:
        recall = overlapping_count / reference_count

    f1_score = 2.0 * ((precision * recall) / (precision + recall + 1e-8))
    return {"f": f1_score, "p": precision, "r": recall}


def greedy_selection(doc_sent_list, abstract_sent_list, summary_size):
    """
    greedily selects top summary_size sentences
    """

    def _rouge_clean(s):
        return re.sub(r'[^a-zA-Z0-9 ]', '', s)

    max_rouge = 0.0
    abstract = sum(abstract_sent_list, [])
    abstract = _rouge_clean(' '.join(abstract)).split()
    sents = [_rouge_clean(' '.join(s)).split() for s in doc_sent_list]
    evaluated_1grams = [_get_word_ngrams(1, [sent]) for sent in sents]
    reference_1grams = _get_word_ngrams(1, [abstract])
    evaluated_2grams = [_get_word_ngrams(2, [sent]) for sent in sents]
    reference_2grams = _get_word_ngrams(2, [abstract])

    selected = []
    for s in range(summary_size):
        cur_max_rouge = max_rouge
        cur_id = -1
        for i in range(len(sents)):
            if i in selected:
                continue
            c = selected + [i]
            candidates_1 = [evaluated_1grams[idx] for idx in c]
            candidates_1 = set.union(*map(set, candidates_1))
            candidates_2 = [evaluated_2grams[idx] for idx in c]
            candidates_2 = set.union(*map(set, candidates_2))
            rouge_1 = cal_rouge(candidates_1, reference_1grams)['f']
            rouge_2 = cal_rouge(candidates_2, reference_2grams)['f']
            rouge_score = rouge_1 + rouge_2
            if rouge_score > cur_max_rouge:
                cur_max_rouge = rouge_score
                cur_id = i
        if cur_id == -1:
            return selected
        selected.append(cur_id)
        max_rouge = cur_max_rouge

    return sorted(selected)


def hashhex(s):
    """Returns a heximal formated SHA1 hash of the input string."""
    h = hashlib.sha1()
    h.update(s.encode('utf-8'))
    return h.hexdigest()


class BertData:
    def __init__(self, args):
        self.args = args
        self.tokenizer = BertTokenizer.from_pretrained('bert-base-uncased', do_lower_case=True)

        self.sep_token = '[SEP]'
        self.cls_token = '[CLS]'
        self.pad_token = '[PAD]'
        self.tgt_bos = '[unused0]'
        self.tgt_eos = '[unused1]'
        self.tgt_sent_split = '[unused2]'
        self.sep_vid = self.tokenizer.vocab[self.sep_token]
        self.cls_vid = self.tokenizer.vocab[self.cls_token]
        self.pad_vid = self.tokenizer.vocab[self.pad_token]

    def preprocess(self, src, sections, tgt, sent_labels, use_bert_basic_tokenizer=False, is_test=False):

        if not is_test and len(src) == 0:
            return None

        original_src_txt = [' '.join(s) for s in src]

        idxs = [i for i, s in enumerate(src) if len(s) > self.args.min_src_ntokens_per_sent]  # indices of sentences that are of minimum length

        _sent_labels = [0] * len(src)
        for l in sent_labels:
            _sent_labels[l] = 1
        # truncate to max tokens and remove short sents
        src = [src[i][:self.args.max_src_ntokens_per_sent] for i in idxs]
        sent_labels = [_sent_labels[i] for i in idxs]
        src = src[:self.args.max_src_nsents]
        sent_labels = sent_labels[:self.args.max_src_nsents]
        _sections = [sections[i] for i in idxs]
        _sections = _sections[:self.args.max_src_nsents]
        if not is_test and len(src) < self.args.min_src_nsents:
            return None

        src_txt = [' '.join(sent) for sent in src]
        text = ' {} {} '.format(self.sep_token, self.cls_token).join(src_txt)

        src_subtokens = self.tokenizer.tokenize(text)

        src_subtokens = [self.cls_token] + src_subtokens + [self.sep_token]
        src_subtoken_idxs = self.tokenizer.convert_tokens_to_ids(src_subtokens)
        # identify end of sents
        _segs = [-1] + [i for i, t in enumerate(src_subtoken_idxs) if t == self.sep_vid]
        # identify length of sents
        segs = [_segs[i] - _segs[i - 1] for i in range(1, len(_segs))]
        token_sections = []  # This array contains 0 for all tokens in the first sections, 1 for all tokens in second section
        segments_ids = []
        # intermittent labeling of sents based on being odd or even
        for i, s in enumerate(segs):
            if i % 2 == 0:
                segments_ids += s * [0]
            else:
                segments_ids += s * [1]
            token_sections += s * [_sections[i]]

        cls_ids = [i for i, t in enumerate(src_subtoken_idxs) if t == self.cls_vid]
        sent_labels = sent_labels[:len(cls_ids)]

        tgt_subtokens_str = '[unused0] ' + ' [unused2] '.join(
            [' '.join(self.tokenizer.tokenize(' '.join(tt), use_bert_basic_tokenizer=use_bert_basic_tokenizer)) for tt
             in tgt]) + ' [unused1]'
        tgt_subtoken = tgt_subtokens_str.split()[:self.args.max_tgt_ntokens]
        if (not is_test) and len(tgt_subtoken) < self.args.min_tgt_ntokens:
            return None

        tgt_subtoken_idxs = self.tokenizer.convert_tokens_to_ids(tgt_subtoken)

        tgt_txt = '<q>'.join([' '.join(tt) for tt in tgt])
        src_txt = [original_src_txt[i] for i in idxs]

        assert (len(token_sections) == len(segments_ids)), f'token sections and segment ids do not have same length'
        assert (len(_sections) == len(sent_labels) == len(cls_ids)), \
            f'prepocessed dimentions do not match {len(_sections)}, {len(sent_labels)}, {len(cls_ids)}'
        return src_subtoken_idxs, sent_labels, tgt_subtoken_idxs, segments_ids, cls_ids, src_txt, tgt_txt, _sections, token_sections


def _get_text_clean_tika(xml_file):
    pages_text = []
    path = xml_file.split('/')
    clean_path = '/'.join(path[:-1]) + '/clean_tika.txt'
    with open(xml_file, "r") as file:
        content = "".join(file.readlines())
        bs_content = bs(content, "lxml")
        for page in bs_content.find_all("div", {"class": "page"}):
            pages_text.append('\n'.join([p.text.strip() for p in page.find_all("p") if p.text]).strip())
    xml_text = '\n'.join(pages_text)

    with open(clean_path, 'w') as file:
        file.write(xml_text)


def get_text_clean_tike(args):
    xmls = []
    for i in range(4984):
        xmls.append('/data/athar/ppt_generation/ppt_generation/slide_generator_data/data/'
                    + str(i) + '/slide.clean_tika.xml')

    pool = Pool(20)
    result = pool.map(_get_text_clean_tika, xmls)
    print(len(result))
    pool.close()
    pool.join()


def clean_paper_jsons(args):
    train_set = []
    test_set = []
    val_set = []
    for i in range(4984):

        main_path = '/data/athar/ppt_generation/ppt_generation/slide_generator_data/data/' + str(i)
        ppt_path = main_path + '/clean_tika.txt.json'
        pdf_path = main_path + '/grobid/sections.stanfordnlp.json'

        if i < 4000:
            train_set.append((pdf_path, ppt_path))
        if 4000 < i < 4250:
            val_set.append((pdf_path, ppt_path))
        if 4250 < i < 4500:
            test_set.append((pdf_path, ppt_path))
    print('train, val, test set sizes are: ', len(train_set), len(val_set), len(test_set))
    corpora = {'train': train_set, 'valid': val_set, 'test': test_set}
    for corpus_type in ['train', 'valid', 'test']:
        pool = Pool(30)
        dataset = []
        p_ct = 0
        size = 0
        for d in pool.imap(load_pdf_ppt_jsons, corpora[corpus_type]):
            dataset.append(d)

            if len(dataset) > args.shard_size:
                pt_file = "{:s}/{:s}.{:d}.json".format(args.save_path, corpus_type, p_ct)

                with open(pt_file, 'w') as save:
                    save.write(json.dumps(dataset))
                    size += len(dataset)
                    p_ct += 1
                    dataset = []

        pool.close()
        pool.join()

        if len(dataset) > 0:
            pt_file = "{:s}/{:s}.{:d}.json".format(args.save_path, corpus_type, p_ct)
            size += len(dataset)
            with open(pt_file, 'w') as save:
                save.write(json.dumps(dataset))
        print('length of ', corpus_type, ' is ', size)


def format_to_bert(args):
    if args.dataset != '':
        datasets = [args.dataset]
    else:
        datasets = ['train', 'valid', 'test']
    for corpus_type in datasets:
        a_lst = []
        for json_f in glob.glob(pjoin(args.raw_path, '*' + corpus_type + '.*.json')):
            real_name = json_f.split('/')[-1]
            a_lst.append((corpus_type, json_f, args, pjoin(args.save_path, real_name.replace('json', 'bert.pt'))))
        print('document for corpus type:', corpus_type, a_lst)
        pool = Pool(args.n_cpus)
        for d in pool.imap(_format_to_bert, a_lst):
            pass

        pool.close()
        pool.join()


def _format_to_bert(params):
    corpus_type, json_file, args, save_file = params
    is_test = corpus_type == 'test'
    if os.path.exists(save_file):
        logger.info('Ignore %s, Already exists' % save_file)
        return

    bert = BertData(args)

    logger.info('Processing %s' % json_file)
    jobs = json.load(open(json_file))
    datasets = []
    for ii, d in enumerate(jobs):
        source, sections, tgt = d['src'], d['sections'], d['tgt']  # fix add section
        # greedily selects the top 3 sentences and labels them as 1
        summary_size = int(0.2 * len(source))

        sent_labels = greedy_selection(source[:args.max_src_nsents], tgt, summary_size)
        # logger.info('Processing %s, %d with %d number of positive samples' % (json_file, ii, len(sent_labels)))
        if args.lower:
            source = [' '.join(s).lower().split() for s in source]
            tgt = [' '.join(s).lower().split() for s in tgt]
        b_data = bert.preprocess(source, sections, tgt, sent_labels,
                                 use_bert_basic_tokenizer=args.use_bert_basic_tokenizer,
                                 is_test=is_test)

        if b_data is None:
            continue
        src_subtoken_idxs, sent_labels, tgt_subtoken_idxs, segments_ids, cls_ids, src_txt, tgt_txt, sections, token_sections = b_data
        b_data_dict = {"src": src_subtoken_idxs, "tgt": tgt_subtoken_idxs,
                       "src_sent_labels": sent_labels, "segs": segments_ids, 'clss': cls_ids,
                       'src_txt': src_txt, "tgt_txt": tgt_txt, "sections": sections, "token_sections": token_sections}
        datasets.append(b_data_dict)
    logger.info('Processed %d instances.' % len(datasets))
    logger.info('Saving to %s' % save_file)
    torch.save(datasets, save_file)
    gc.collect()


def format_to_lines(args):
    corpus_mapping = {}
    for corpus_type in ['valid', 'test', 'train']:
        temp = []
        for line in open(pjoin(args.map_path, 'mapping_' + corpus_type + '.txt')):
            temp.append(hashhex(line.strip()))
        corpus_mapping[corpus_type] = {key.strip(): 1 for key in temp}
    train_files, valid_files, test_files = [], [], []
    for f in glob.glob(pjoin(args.raw_path, '*.json')):
        real_name = f.split('/')[-1].split('.')[0]
        if real_name in corpus_mapping['valid']:
            valid_files.append(f)
        elif real_name in corpus_mapping['test']:
            test_files.append(f)
        elif real_name in corpus_mapping['train']:
            train_files.append(f)
        # else:
        #     train_files.append(f)

    corpora = {'train': train_files, 'valid': valid_files, 'test': test_files}
    for corpus_type in ['train', 'valid', 'test']:
        a_lst = [(f, args) for f in corpora[corpus_type]]
        pool = Pool(args.n_cpus)
        dataset = []
        p_ct = 0
        for d in pool.imap_unordered(_format_to_lines, a_lst):
            dataset.append(d)
            if len(dataset) > args.shard_size:
                pt_file = "{:s}.{:s}.{:d}.json".format(args.save_path, corpus_type, p_ct)
                with open(pt_file, 'w') as save:
                    # save.write('\n'.join(dataset))
                    save.write(json.dumps(dataset))
                    p_ct += 1
                    dataset = []

        pool.close()
        pool.join()
        if len(dataset) > 0:
            pt_file = "{:s}.{:s}.{:d}.json".format(args.save_path, corpus_type, p_ct)
            with open(pt_file, 'w') as save:
                # save.write('\n'.join(dataset))
                save.write(json.dumps(dataset))
                p_ct += 1
                dataset = []


def _format_to_lines(params):
    f, args = params
    print(f)
    source, sections, tgt = load_json(f, args.lower)
    return {'src': source, 'sections': sections, 'tgt': tgt}


def format_xsum_to_lines(args):
    if args.dataset != '':
        datasets = [args.dataset]
    else:
        datasets = ['train', 'test', 'valid']

    corpus_mapping = json.load(open(pjoin(args.raw_path, 'XSum-TRAINING-DEV-TEST-SPLIT-90-5-5.json')))

    for corpus_type in datasets:
        mapped_fnames = corpus_mapping[corpus_type]
        root_src = pjoin(args.raw_path, 'restbody')
        root_tgt = pjoin(args.raw_path, 'firstsentence')
        # realnames = [fname.split('.')[0] for fname in os.listdir(root_src)]
        realnames = mapped_fnames

        a_lst = [(root_src, root_tgt, n) for n in realnames]
        pool = Pool(args.n_cpus)
        dataset = []
        p_ct = 0
        for d in pool.imap_unordered(_format_xsum_to_lines, a_lst):
            if d is None:
                continue
            dataset.append(d)
            if len(dataset) > args.shard_size:
                pt_file = "{:s}.{:s}.{:d}.json".format(args.save_path, corpus_type, p_ct)
                with open(pt_file, 'w') as save:
                    save.write(json.dumps(dataset))
                    p_ct += 1
                    dataset = []

        pool.close()
        pool.join()
        if len(dataset) > 0:
            pt_file = "{:s}.{:s}.{:d}.json".format(args.save_path, corpus_type, p_ct)
            with open(pt_file, 'w') as save:
                save.write(json.dumps(dataset))
                p_ct += 1
                dataset = []


def _format_xsum_to_lines(params):
    src_path, root_tgt, name = params
    f_src = pjoin(src_path, name + '.restbody')
    f_tgt = pjoin(root_tgt, name + '.fs')
    if os.path.exists(f_src) and os.path.exists(f_tgt):
        print(name)
        source = []
        for sent in open(f_src):
            source.append(sent.split())
        tgt = []
        for sent in open(f_tgt):
            tgt.append(sent.split())
        return {'src': source, 'tgt': tgt}
    return None
