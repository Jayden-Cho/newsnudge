import numpy as np
from sklearn.metrics import roc_auc_score
from tqdm import tqdm
import torch
from config import model_name
from google.cloud import storage
from torch.utils.data import Dataset, DataLoader
from data_preprocess import data_process
import os
from os import path
import sys
import pandas as pd
from ast import literal_eval
import importlib
from multiprocessing import Pool
import csv
import tempfile 
import pandas as pd

import smtplib
from email.message import EmailMessage
from datetime import date

SMTP_SERVER = 'smtp.gmail.com'
SMTP_PORT = 587
EMAIL_ADDR = 'tjdrms2023@gmail.com'
APP_PASSWORD = 'dswjergpnowsylaj'

try:
    Model = getattr(importlib.import_module(f"model.{model_name}"), model_name)
    config = getattr(importlib.import_module('config'), f"{model_name}Config")
except AttributeError:
    print(f"{model_name} not included!")
    exit()

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


class NewsDataset(Dataset):
    """
    Load news for prediction.
    """
    def __init__(self, news_path):
        super(NewsDataset, self).__init__()
        self.news_predict_parsed = pd.read_table(
            news_path,
            usecols=['id'] + config.dataset_attributes['news'],
            converters={
                attribute: literal_eval
                for attribute in set(config.dataset_attributes['news']) & set([
                    'title', 'abstract'])
            })
        self.news2dict = self.news_predict_parsed.to_dict('index')
        for key1 in self.news2dict.keys():
            for key2 in self.news2dict[key1].keys():
                if type(self.news2dict[key1][key2]) != str:
                    self.news2dict[key1][key2] = torch.tensor(
                        self.news2dict[key1][key2])

    def __len__(self):
        return len(self.news_predict_parsed)

    def __getitem__(self, idx):
        item = self.news2dict[idx]
        return item


class UserDataset(Dataset):
    """
    Load users for evaluation, duplicated rows will be dropped
    """
    def __init__(self, behaviors_path, user2int_path):
        super(UserDataset, self).__init__()
        self.behaviors = pd.read_table(behaviors_path,
                                       header=None,
                                       usecols=[1, 3],
                                       names=['user', 'clicked_news'])
        self.behaviors.clicked_news.fillna(' ', inplace=True)
        self.behaviors.drop_duplicates(inplace=True)
        user2int = dict(pd.read_table(user2int_path).values.tolist())
        user_total = 0
        user_missed = 0
        for row in self.behaviors.itertuples():
            user_total += 1
            if row.user in user2int:
                self.behaviors.at[row.Index, 'user'] = user2int[row.user]
            else:
                user_missed += 1
                self.behaviors.at[row.Index, 'user'] = 0

    def __len__(self):
        return len(self.behaviors)

    def __getitem__(self, idx):
        row = self.behaviors.iloc[idx]
        item = {
            "user":
            row.user,
            "clicked_news_string":
            row.clicked_news,
            "clicked_news":
            row.clicked_news.split()[:config.num_clicked_news_a_user]
        }
        item['clicked_news_length'] = len(item["clicked_news"])
        repeated_times = config.num_clicked_news_a_user - len(
            item["clicked_news"])
        assert repeated_times >= 0
        item["clicked_news"] = ['PADDED_NEWS'
                                ] * repeated_times + item["clicked_news"]

        return item


class BehaviorsDataset(Dataset):
    """
    Load behaviors for evaluation, (user, time) pair as session
    """
    def __init__(self, behaviors_path):
        super(BehaviorsDataset, self).__init__()
        self.behaviors = pd.read_table(behaviors_path,
                                       header=None,
                                       usecols=range(5),
                                       names=[
                                           'impression_id', 'user', 'time',
                                           'clicked_news', 'impressions'
                                       ])
        self.behaviors.clicked_news.fillna(' ', inplace=True)
        self.behaviors.impressions = self.behaviors.impressions.str.split()

    def __len__(self):
        return len(self.behaviors)

    def __getitem__(self, idx):
        row = self.behaviors.iloc[idx]
        item = {
            "impression_id": row.impression_id,
            "user": row.user,
            "time": row.time,
            "clicked_news_string": row.clicked_news,
            "impressions": row.impressions
        }
        return item

@torch.no_grad()
def predict(model, directory, num_workers, max_count=sys.maxsize):
    """
    Predict model on target directory.
    Args:
        model: model to be predicted
        directory: the directory that contains two files (behaviors.tsv, news_predict_parsed.tsv)
        num_workers: processes number for calculating metrics
    Returns:
        AUC
        MRR
        nDCG@5
        nDCG@10
    """
    news_dataset = NewsDataset(path.join(directory, 'news_parsed.tsv'))
    news_dataloader = DataLoader(news_dataset,
                                 batch_size=config.batch_size,
                                 shuffle=False,
                                 num_workers=config.num_workers,
                                 drop_last=False,
                                 pin_memory=True)

    news2vector = {}
    for minibatch in tqdm(news_dataloader,
                          desc="Calculating vectors for news"):
        news_ids = minibatch['id']
        if any(id not in news2vector for id in news_ids):
            news_vector = model.get_news_vector(minibatch)
            for id, vector in zip(news_ids, news_vector):
                if id not in news2vector:
                    news2vector[id] = vector

    news2vector['PADDED_NEWS'] = torch.zeros(
        list(news2vector.values())[0].size())
    
    print('check if news2vector works properly:',news2vector.items())

    user_dataset = UserDataset(path.join(directory, 'behaviors.tsv'),
                               path.join(directory, 'user2int.tsv'))
    user_dataloader = DataLoader(user_dataset,
                                 batch_size=config.batch_size,
                                 shuffle=False,
                                 num_workers=config.num_workers,
                                 drop_last=False,
                                 pin_memory=True)
    
    user2vector = {}
    for minibatch in tqdm(user_dataloader,
                          desc="Calculating vectors for users"):
        user_strings = minibatch["clicked_news_string"]
        if any(user_string not in user2vector for user_string in user_strings):
            clicked_news_vector = torch.stack([
                torch.stack([news2vector[x].to(device) for x in news_list],
                            dim=0) for news_list in minibatch["clicked_news"]
            ],
                                              dim=0).transpose(0, 1)
            user_vector = model.get_user_vector(clicked_news_vector)
            for user, vector in zip(user_strings, user_vector):
                if user not in user2vector:
                    user2vector[user] = vector

    print('check if user2vector works properly:', user2vector.items())

    behaviors_dataset = BehaviorsDataset(path.join(directory, 'behaviors.tsv'))
    behaviors_dataloader = DataLoader(behaviors_dataset,
                                      batch_size=1,
                                      shuffle=False,
                                      num_workers=config.num_workers)

    count = 0

    for minibatch in tqdm(behaviors_dataloader,
                          desc="Calculating probabilities"):
        count += 1
        if count == max_count:
            break

        news_index = [news[0] for news in minibatch['impressions']]
        candidate_news_vector = torch.stack([news2vector[news[0]] for news in minibatch['impressions']], dim=0)
        user_vector = user2vector[minibatch['clicked_news_string'][0]]
        click_probability = model.get_prediction(candidate_news_vector,
                                                 user_vector)

        y_pred = click_probability.tolist()

        prediction = {news_index[i]: y_pred[i] for i in range(len(news_index))}

        news = pd.read_table(path.join(directory, 'news.tsv'),
                            header=0,
                            usecols=[0, 2, 6, 7],
                            quoting=csv.QUOTE_NONE,
                            names=[
                                'id', 'category', 'title',
                                'abstract'
                            ])
        news.fillna(' ', inplace=True)             

        category_to_news = {}

        for news_id, prediction_value in prediction.items():
            category = news[news['id']==news_id]['category'].iloc[0]
            if category not in category_to_news or prediction_value > prediction[category_to_news[category]]:
                category_to_news[category] = news_id

    return category_to_news

def empty_dataframe():
    file_path = 'gs://newsnudge/data/predict' 
    df = pd.read_table(path.join(file_path, 'news.tsv'))
    empty_df = pd.DataFrame(columns=df.columns)

    bucket_name = 'newsnudge'
    blob_name = 'data/predict/news.tsv'

    storage_client = storage.Client()
    bucket = storage_client.get_bucket(bucket_name)

    bucket.blob(blob_name).upload_from_string(empty_df.to_csv(index=False, sep='\t', encoding='utf-8'), 'text/tab-separated-values') 

'''
{'Politics': 'N3', 'Economics': 'N10', 'Social': 'N29', 'Life/Cultures': 'N34', 'World': 'N40', 'IT/Science': 'N48'}
'''

def write_email_content(recommendations, file_path):
    news = pd.read_table(path.join(file_path, 'news.tsv'),
                        header=0,
                        usecols=[0, 2, 6, 7],
                        quoting=csv.QUOTE_NONE,
                        names=[
                            'index', 'category', 'title',
                            'abstract'
                        ])
    content = ''
    for category in recommendations:
        article = news[news['index'] == recommendations[category]].iloc[0]
        content += article['category'] + '\n' + article['title'] + '\n' + article['abstract'] + '\n\n'

    return content

def send_email(content):
    # Create a SMTP object with server name and port number
    smtp = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)

    # Set up server connection
    smtp.ehlo()

    # Encrypt the connection for privacy issues
    smtp.starttls()

    # Login with email information
    smtp.login(EMAIL_ADDR, APP_PASSWORD)

    # Create an email object and its contents
    msg = EmailMessage()
    msg['Subject'] = str(date.today()) + ' Breaking News Today by NewsNudge'
    msg.set_content(content)
    msg['From'] = EMAIL_ADDR
    msg['To'] = EMAIL_ADDR

    smtp.send_message(msg)

def make_prediction(request):
    print('Using device:', device)
    print(f'Evaluating model {model_name}')

    model = Model(config).to(device)

    from train import latest_checkpoint 
    checkpoint_path = latest_checkpoint(path.join('./checkpoint', model_name))

    if checkpoint_path is not None:
        checkpoint = torch.load(checkpoint_path)
        model.load_state_dict(checkpoint['model_state_dict'])

    model.eval()
    data_process()
    file_path = 'gs://newsnudge/data/predict' 
    recommendations = predict(model, file_path, config.num_workers) # recommendations = predict(model, './data/predict', config.num_workers)
    print(recommendations)
    print('Recommendation process finished. Started to write an email content.')
    content = write_email_content(recommendations, file_path)
    print('content has been written. Now is to send an email')
    send_email(content)
    print('email has been sent.')
    # empty_dataframe()
    
    return "Recommendation successful!"