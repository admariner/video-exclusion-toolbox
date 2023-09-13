# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Pull YouTube video data for the placements in the Google Ads report."""
import base64
import datetime
import hashlib
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List

from google.api_core import exceptions
import google.auth
import google.auth.credentials
from google.cloud import bigquery
from googleapiclient import discovery
import jsonschema
import numpy as np
import pandas as pd
from utils import bq

logging.basicConfig(stream=sys.stdout)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# The Google Cloud project
GOOGLE_CLOUD_PROJECT = os.environ.get('GOOGLE_CLOUD_PROJECT')
# The bucket to write the data to
VID_EXCL_GCS_DATA_BUCKET = os.environ.get('VID_EXCL_GCS_DATA_BUCKET')
# The name of the BigQuery Dataset
BQ_DATASET = os.environ.get('VID_EXCL_BIGQUERY_DATASET')

# Optional variable to specify the problematic CSV characters. This is an env
# variable, so if any other characters come up they can be replaced in the
# Cloud Function UI without redeploying the solution.
VID_EXCL_CSV_PROBLEM_CHARACTERS_REGEX = os.environ.get(
    'VID_EXCL_CSV_PROBLEM_CHARACTERS', r'\$|\"|\'|\r|\n|\t|,|;|:'
)
# Maximum number of channels per YouTube request. See:
# https://developers.google.com/youtube/v3/docs/channels/list
CHUNK_SIZE = 50

# The schema of the JSON in the event payload
message_schema = {
    'type': 'object',
    'properties': {
        'customer_id': {'type': 'string'},
        'blob_name': {'type': 'string'},
    },
    'required': [
        'blob_name',
        'customer_id',
    ],
}


def main(event: Dict[str, Any], context: Dict[str, Any]) -> None:
  """The entry point: extract the data from the payload and starts the job.

  The pub/sub message must match the message_schema object above.

  Args:
      event: A dictionary representing the event data payload.
      context: An object containing metadata about the event.

  Raises:
      jsonschema.exceptions.ValidationError if the message from pub/sub is not
      what is expected.
  """
  del context
  logger.info('YouTube video service triggered.')
  logger.info('Message: %s', event)
  message = base64.b64decode(event['data']).decode('utf-8')
  logger.info('Decoded message: %s', message)
  message_json = json.loads(message)
  logger.info('JSON message: %s', message_json)

  # Will raise jsonschema.exceptions.ValidationError if the schema is invalid
  jsonschema.validate(instance=message_json, schema=message_schema)

  run(message_json.get('blob_name'))

  logger.info('Done')


def run(blob_name: str) -> None:
  """Orchestration to pull YouTube data and output it to BigQuery.

  Args:
      blob_name: The name of the newly created account report file.
  """
  credentials = get_auth_credentials()

  # step 1: Pull list of YT Video IDs from the blob
  # Open blob and get specifc column
  data = pd.read_csv(f'gs://{VID_EXCL_GCS_DATA_BUCKET}/{blob_name}')
  video_ids = data[['video_id']].drop_duplicates()
  # for channel_ids not in BQ, run the following
  logger.info('Checking new videos')
  logger.info('Connecting to: %s BigQuery', GOOGLE_CLOUD_PROJECT)
  client = bigquery.Client(
      project=GOOGLE_CLOUD_PROJECT, credentials=credentials
  )
  temp_table = temp_table_from_csv(video_ids, client)
  logger.info('Filtering previously processed videos.')
  query = f"""
            SELECT video_id
            FROM
            `{BQ_DATASET}.{temp_table}`
            WHERE
            video_id NOT IN (SELECT video_id FROM `{BQ_DATASET}.YouTubeVideo`)
            """
  rows = client.query(query).result()
  video_ids_to_check = []
  for row in rows:
    video_ids_to_check.append(row.video_id)
  logger.info('Found %d new video_ids', len(video_ids_to_check))

  if video_ids_to_check:
    get_youtube_videos_dataframe(video_ids_to_check, credentials)
  else:
    logger.info('No new video IDs to process')
  logger.info('Done')


def get_auth_credentials() -> google.auth.credentials.Credentials:
  """Returns credentials for Google APIs."""
  credentials, _ = google.auth.default()
  return credentials


def temp_table_from_csv(df: pd.DataFrame, client: bigquery.Client) -> str:
  """Creates a temporary BQ table to store video IDs for querying.

  Args:
      df: A dataframe containign the video IDs to be written.
      client: A BigQuery client object.

  Returns:
      The name of the temporary table.
  """

  timestamp = datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
  suffix = hashlib.sha256(timestamp.encode('utf-8')).hexdigest()[:6]
  table_name = '-'.join(['temp-video-ids', suffix, timestamp])
  destination = '.'.join([GOOGLE_CLOUD_PROJECT, BQ_DATASET, table_name])
  logger.info('Creating a temporary table: %s', table_name)

  job_config = bigquery.LoadJobConfig(
      schema=[
          bigquery.SchemaField('video_id', bigquery.enums.SqlTypeNames.STRING),
      ],
      write_disposition='WRITE_TRUNCATE',
  )

  job = client.load_table_from_dataframe(
      dataframe=df, destination=destination, job_config=job_config
  )
  job.result()

  expiration = datetime.datetime.now(
      datetime.timezone.utc
  ) + datetime.timedelta(hours=1)

  # This is not elegant, but necessary. BQ seems to sometimes refuse to update
  # table's metadata shortly after creating it. Retrying after a few seconds
  # is a crude, but working workaround.
  try:
    table = client.get_table(destination)
    table.expires = expiration
    client.update_table(table, ['expires'])
  except exceptions.PreconditionFailed:
    logger.info(
        "Failed to update expiration for table '%s' wating 5 seconds and"
        ' retrying.',
        table_name
    )
    time.sleep(5)
    table = client.get_table(destination)
    table.expires = expiration
    client.update_table(table, ['expires'])

  logger.info("Table '%s' created.", table_name)

  return table_name


def get_youtube_videos_dataframe(
    video_ids: List[str],
    credentials: google.auth.credentials.Credentials,
) -> None:
  """Pulls information on each of the videos provided from the YouTube API.

  The YouTube API only allows pulling up to 50 videos in each request, so
  multiple requests have to be made to pull all the data. See the docs for
  more details:
  https://developers.google.com/youtube/v3/docs/channels/list

  Args:
      video_ids: The video IDs to pull the info on from YouTube.
      credentials: Google Auth credentials.
  """
  logger.info('Getting YouTube data for video IDs')

  chunks = split_list_to_chunks(video_ids, CHUNK_SIZE)
  number_of_chunks = len(chunks)

  logger.info('Connecting to the youtube API')
  youtube = discovery.build('youtube', 'v3', credentials=credentials)
  all_videos = []
  for i, chunk in enumerate(chunks):
    logger.info('Processing chunk %s of %s', i + 1, number_of_chunks)
    chunk_list = list(chunk)
    request = youtube.videos().list(
        part='id,snippet,contentDetails,statistics',
        id=chunk_list,
        maxResults=CHUNK_SIZE,
    )
    response = request.execute()
    videos = process_youtube_videos_response(response, chunk_list)
    for video in videos:
      all_videos.append(video)
  youtube_df = pd.DataFrame(
      all_videos,
      columns=[
          'video_id',
          'title',
          'description',
          'publishedAt',
          'channelId',
          'categoryId',
          'tags',
          'defaultLanguage',
          'duration',
          'definition',
          'licensedContent',
          'ytContentRating',
          'viewCount',
          'likeCount',
          'commentCount',
      ],
  )
  youtube_df['datetime_updated'] = datetime.datetime.now()
  youtube_df = sanitise_youtube_dataframe(youtube_df)
  write_results_to_bq(youtube_df, BQ_DATASET + '.YouTubeVideo')
  logger.info('YouTube Video info complete')


def sanitise_youtube_dataframe(youtube_df: pd.DataFrame) -> pd.DataFrame:
  """Takes the dataframe from YouTube and sanitises it to write as a CSV.

  Args:
      youtube_df: The dataframe containing the YouTube data.

  Returns:
      The YouTube dataframe but sanitised to be safe to write to a CSV.
  """
  youtube_df['viewCount'] = youtube_df['viewCount'].fillna(0)
  youtube_df['likeCount'] = youtube_df['likeCount'].fillna(0)
  youtube_df['commentCount'] = youtube_df['commentCount'].fillna(0)
  youtube_df['categoryId'] = youtube_df['categoryId'].fillna(0)
  youtube_df['publishedAt'] = pd.to_datetime(
      youtube_df['publishedAt'], errors='coerce'
  )
  youtube_df['publishedAt'] = youtube_df['publishedAt'].dt.tz_localize(None)
  try:
    youtube_df = youtube_df.astype({
        'viewCount': 'Int64',
        'likeCount': 'Int64',
        'commentCount': 'Int64',
        'categoryId': 'Int64',
        'publishedAt': 'datetime64[ns]',
    })
  except TypeError:
    logger.info('Unable to sanitise DataFrame:')
    logger.info(youtube_df)
    raise

  # remove problematic characters from the title field as the break BigQuery
  # even when escaped in the CSV
  youtube_df['title'] = youtube_df['title'].str.replace(
      VID_EXCL_CSV_PROBLEM_CHARACTERS_REGEX, '', regex=True
  )
  youtube_df['title'] = youtube_df['title'].str.strip()
  youtube_df['description'] = youtube_df['description'].str.replace(
      VID_EXCL_CSV_PROBLEM_CHARACTERS_REGEX, '', regex=True
  )
  youtube_df['description'] = youtube_df['description'].str.strip()
  return youtube_df


def split_list_to_chunks(
    data: List[Any], max_size_of_chunk: int
) -> List[np.ndarray]:
  """Splits the list into X chunks with the maximum size as specified.

  Args:
      data: The list to be split into chunks.
      max_size_of_chunk: The maximum number of elements that should be in a
        chunk.

  Returns:
      A list containing numpy array chunks of the original list.
  """
  logger.info('Splitting data into chunks')
  num_of_chunks = (len(data) + max_size_of_chunk - 1) // max_size_of_chunk
  chunks = np.array_split(data, num_of_chunks)
  logger.info('Split list into %i chunks', num_of_chunks)
  return chunks


def process_youtube_videos_response(
    response: Dict[str, Any], video_ids: List[str]
) -> List[List[Any]]:
  """Processes the YouTube response to extract the required information.

  Args:
      response: The YouTube video list response
          https://developers.google.com/youtube/v3/docs/channels/list#response.
      video_ids: A list of the video IDs passed in the request.

  Returns:
      A list of dicts where each dict represents data from one channel.
  """
  logger.info('Processing youtube response')
  data = []
  if response.get('pageInfo').get('totalResults') == 0:
    logger.warning('The YouTube response has no results: %s', response)
    logger.warning(video_ids)
    return data

  for video in response['items']:
    data.append([
        video.get('id'),
        video['snippet'].get('title', ''),
        video['snippet'].get('description', None),
        video['snippet'].get('publishedAt', None),
        video['snippet'].get('channelId', None),
        video['snippet'].get('categoryId', None),
        video['snippet'].get('tags', None),
        video['snippet'].get('defaultLanguage', ''),
        video['contentDetails'].get('duration', ''),
        video['contentDetails'].get('definition', ''),
        video['contentDetails'].get('licensedContent', ''),
        video['contentDetails'].get('contentRating').get('ytRating', ''),
        video['statistics'].get('viewCount', None),
        video['statistics'].get('likeCount', None),
        video['statistics'].get('commentCount', None),
    ])
  return data


def write_results_to_bq(
    youtube_df: pd.DataFrame, table_id: str
) -> None:
  """Writes the YouTube dataframe to BQ.

  Args:
      youtube_df: The dataframe based on the YouTube data.
      table_id: The id of the BQ table.
  """
  logger.info('Writing results to BQ: %s', table_id)
  number_of_rows = len(youtube_df.index)
  logger.info('There are %s rows', number_of_rows)
  if number_of_rows > 0:
    bq.load_to_bq_from_df(
        df=youtube_df, table_id=table_id
    )
    logger.info('YT data added to BQ table')
  else:
    logger.info('There is nothing to write to BQ')
