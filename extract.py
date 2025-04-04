import torch
from jsonargparse import ArgumentParser
import transformers
import sys
import time
import json
import traceback
import pickle
#from transformers.pipelines.pt_utils import KeyDataset
import datetime
import datasets
import os
from sentence_transformers import SentenceTransformer
import pandas as pd


from datasets.utils.logging import disable_progress_bar      #prevent the libraries from displaying a progress bar
disable_progress_bar()

from datasets.utils.logging import set_verbosity_error
set_verbosity_error()                                        #prevent the libraries' messages from being displayed


#------------------------------- Define aliases for models here ------------------------------- #
model_name_dict = {"e5": "intfloat/multilingual-e5-large-instruct",
                   "qwen" : "Alibaba-NLP/gte-Qwen2-7B-instruct",
                   "jina" : "jinaai/jina-embeddings-v3"
}

#-------------------------------- Define possible prompts here -------------------------------- #

def get_task_def_by_task_name_and_type(task_type: str) -> str:
    if task_type in ['STS']:
        return "Retrieve semantically similar text."
    if task_type in ['Summarization']:
        return "Given a summary, retrieve other semantically similar summaries"
    if task_type in ['BitextMining']:
        return "Retrieve parallel sentences."    # this needs to be adjusted fron qwen
    if task_type in ['Retrieval']:
        return "Given a web search query, retrieve relevant passages that answer the query"
    raise ValueError(f"No instruction config for task {task_type}")

def get_detailed_instruct(task_description: str, query: str) -> str:
    return f'Instruct: {task_description}\nQuery: {query}'

def get_query(task_name:str, text:str) -> list:
    # get the task explanation
    task = get_task_def_by_task_name_and_type(task_name)
    # combine task description and text
    return get_detailed_instruct(task, text)

#--------------------------------- Chuncking of long documents --------------------------------- #

def text2dataset(line,chunk_size=5000):
    """
    turn a text into a dataset of text chunks
    """
    txt = line["text"]
    chunks=[]
    offsets=[]
    print(line)
    for chunk_offset in range(0,len(txt),chunk_size):
        #print(f'[{chunk_offset}:{chunk_offset+chunk_size}]')
        chunks.append(txt[chunk_offset:chunk_offset+chunk_size])
        offsets.append(chunk_offset)
    return datasets.Dataset.from_dict({"text":chunks, "id":[line["id"]]*len(chunks), "register": [line["register"]]*len(chunks), "offset": offsets})

def text2chunks(line,chunk_size=5000):
    """
    turn a text into chunked segments
    """
    txt = line["text"]
    chunks=[]
    offsets=[]
    for chunk_offset in range(0,len(txt),chunk_size):
        chunks.append(txt[chunk_offset:chunk_offset+chunk_size])
        offsets.append(chunk_offset)
    #return {"text":chunks, "id":[line["id"]]*len(chunks), "register": [line["register"]]*len(chunks), "offset": offsets}
    return chunks, [line["id"]]*len(chunks), [line["register"]]*len(chunks), offsets

#-------------------------------------- Old tested options -------------------------------------- #

def using_pandas(df, model):
    """
    Use pandas as the data structure instead of vectors; 
    not used because this because dumping pandas tables was harder than jsonls.
    """
    input_texts = df["text"].tolist()
    embeddings = model.encode(input_texts, convert_to_tensor=False, normalize_embeddings=False)
    df["embeddings"] = [row.tolist() for row in embeddings]
    return df


def using_pandas_batch(df, model, batch_size=50):
    """
    Use pandas to do the batching instead of the reading in batches
    """
    input_texts = df["text"].tolist()
    # split the input texts into smaller batches
    input_text_batches = np.array_split(input_texts, len(input_texts) // batch_size + 1)
    embeddings = []
    
    # process each batch
    for batch in input_text_batches:
        batch_embeddings = model.encode(batch, convert_to_tensor=False, normalize_embeddings=False)
        embeddings.extend(batch_embeddings)
    # add the embeddings 
    df["embeddings"] = [row.tolist() for row in embeddings]
    
    return df

#------------------------------------- Pickle dump results ------------------------------------- #   

def pickle_dump_wrt_id(f, ids, offsets, labels, texts, embeddings):
    """
    pickle datasets wrt the id: when calling pickle.load(f), one whole document is returned.
    (as opposed to one segment of a document or one batch of documents)
    """
    data_to_dump = []
    previous_id = None
    
    for i, o, r, t, e in zip(ids, offsets, labels, texts, embeddings): 
        if previous_id is not None and i != previous_id:  # if ID changes, dump the current data
            pickle.dump(data_to_dump, f)
            data_to_dump = []  # reset for new ID
        
        data_to_dump.append({"id": i, "offset": o, "register": r, "text": t, "embeddings": e})
        previous_id = i
    
    # dump the leftovers (should be the last id)
    if data_to_dump:
        pickle.dump(data_to_dump, f)

#------------------------------------ Embedding calculation ------------------------------------ #

def embed(model, texts, options):
    input_texts = [get_query(options.task, t) for t in texts]
    embedded_texts = model.encode(input_texts, convert_to_tensor=False, normalize_embeddings=False)
    return embedded_texts

#------------------------------------------ Main loop ------------------------------------------ # 

def transform(f, options):
    """
    read input from sys.stdin and calculate embeddings for them.
    batch_size controls how many documents (or document segments if chunk_size is set) are handled 
    at the same time.
    Dumped to a pickle document id-wise
    """

    # find model with alias or full name
    model = SentenceTransformer(model_name_dict.get(options.model, options.model),trust_remote_code=True)
    if options.model in ["qwen","Alibaba-NLP/gte-Qwen2-7B-instruct"]:
        model.max_seq_length = 8192

    texts = []
    ids = []
    labels = []
    offsets = []
    for idx, line in enumerate(sys.stdin):
        if options.debug: print(f"In document {idx}", flush=True)
        try: 
            j = json.loads(line)
            if options.chunk_size:
                chunk, id_, label, offset  = text2chunks(j, options.chunk_size)
                texts.extend(chunk)
                ids.extend(id_)
                labels.extend(label)
                offsets.extend(offset)
            else:
                texts.append(j["text"])
                ids.append(j["id"])
                labels.append(j["register"])
                offsets.append(j["offset"])
        except:
            print(f'Problem with text on idx {idx}')
            traceback.print_exc()
            print("")

        if len(texts) >= options.batch_size:
            if options.debug: print(f"Doing a batch at index {idx}")
            embedded_texts = embed(model, texts, options)
            pickle_dump_wrt_id(f, ids, offsets, labels, texts, embedded_texts)
            
            # re-init
            texts = []
            ids = []
            labels = []
            offsets = []

    if len(ids) > 0:   # we have leftovers; e.g. last chunk was not over batch size
        if options.debug: print("Dumping leftovers")
        embedded_texts = embed(model, texts, options)
        pickle_dump_wrt_id(f, ids, offsets, labels, texts, embedded_texts)
        

#-------------------------------------------- Start -------------------------------------------- # 


parser = ArgumentParser(prog="extract.py")
#parser.add_argument('--data',type=str,help="Path to dataset")
parser.add_argument('--model',type=str,help="Model name")
parser.add_argument('--save', type=str,help="Path for saving results", default=None)
parser.add_argument('--task', default="STS", choices=["STS","Summarization","BitextMining","Retrieval"], help='Task (==which query to use)')
parser.add_argument('--chunk_size', '--chunksize',type=int,help="elements per batch", default = None)
parser.add_argument('--batch_size', '--batchsize', type=int,help="How many files are handled the same time", default = 4)
parser.add_argument('--debug', type=bool, default=False, help="Verbosity etc.")

options = parser.parse_args()
print(options)

save_file = "testi.pkl" if options.save is None else options.save
assert ".pkl" in save_file, "Include a valid path with .pkl in the end"

if os.path.dirname(save_file):
    os.makedirs(os.path.dirname(save_file), exist_ok=True)

with open(save_file, "wb") as f:
    transform(f, options)