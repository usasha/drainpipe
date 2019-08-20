import logging
import os
import random
import sys
import time
from typing import List

import pandas as pd
import redis

help_message = 'Hello, please pass pattern to match and path to CSV file to dump.'
logger = logging.getLogger('drainpipe')


class StreamDumper:
    """
    Class to parse multiple Redis Streams and persist all new data to one CSV file.
    Parser will use header from CSV file if exists or keys from first updated stream,
    once initialized no new fields will be addad.
    """
    def __init__(self, redis: redis.client.Redis, pattern: str, log_path: str,
                 consumer_group: str = 'default', consumer_name: str = 'default') -> None:
        """
        Create new stream dumper.
        :param redis: redis connection
        :param pattern: patter to match, use * for wildcard
        :param log_path: path to CSV file to store new messages
        :param consumer_group: consumer group name, usefull when you want to use more than ane drainpipe on stream
        to strore updates to more than one file (e.g. streams 1, 2, 3 -> small.csv; 2, 4, 6 -> even.csv)
        :param consumer_name: consumer name, used to be able to run multiple replicas of same drainpipes for scaling
        """
        logger.debug('init drainpipe')
        self.redis = redis
        self.pattern = pattern
        self.log_path = log_path
        self.stream_cursor = dict()
        self.consumer_group = consumer_group
        self.consumer_name = consumer_name

        try:
            with open(self.log_path, 'r') as f:
                self.header = [word for word in f.readline().strip().split(',') if word]
                logger.debug('header inferred from file: %s', self.header)
        except FileNotFoundError:
            self.header = []
        logger.info('drainpipe initialized, pattern: %s, CSV: %s', self.pattern,  self.log_path)

    @staticmethod
    def find_header(stream_content: List) -> List[str]:
        """
        Infer CSV header based on keys in first found message
        :param stream_content: unmodified response of redis.Redis().xreadgroup()
        :return: list of columns started with 'stream' and 'timestamp' and that decoded stream fields
        """
        columns = [column.decode() for column in stream_content[0][1][0][1].keys()]
        if columns:
            logger.debug('header inferred from stream: %s', ['stream', 'timestamp'] + columns)
            return ['stream', 'timestamp'] + columns
        else:
            logger.debug('header not found in stream')
            return []

    def consume_streams(self) -> None:
        """
        Check for new streams matching self.pattern, start track if any.
        Check for updates in tracked streams, dump new messages to CSV.
        """
        _, streams = self.redis.scan(match=self.pattern, count=int(10e10))  # somehow None option don't work
        for stream in streams:
            if stream not in self.stream_cursor:
                logger.info('new stream found: %s', stream)
                try:
                    self.stream_cursor[stream] = '>'
                    self.redis.xgroup_create(stream, self.consumer_group)
                except redis.exceptions.ResponseError:
                    pass
        if not self.stream_cursor:
            return

        result = self.redis.xreadgroup(self.consumer_group, self.consumer_name, self.stream_cursor, noack=True)
        if not self.header and result:
            self.header = self.find_header(result)
            with open(self.log_path, 'w') as f:
                f.write(','.join(self.header) + '\n')

        for stream, content in result:
            df = pd.DataFrame(content, columns=['timestamp', 'content'])
            timestamp = df['timestamp'].apply(lambda t: int(t.decode().split('-')[0]) // 1000)
            df['timestamp'] = timestamp
            df['stream'] = stream.decode()

            for column in self.header:
                if column not in ['stream', 'timestamp']:
                    df[column] = df['content'].apply(lambda c: c.get(column.encode(), b'').decode())

            df[self.header].to_csv(self.log_path, mode='a', index=False, header=None)
            logger.debug('dumped %s stream up to %s', stream, timestamp)


if __name__ == '__main__':
    try:
        _, pattern, file_name = sys.argv
    except ValueError:
        print(help_message)
        logger.error('no arguments provided, going to exit now')
        sys.exit()

    path_to_csv = f'data/{file_name}'
    redis_host = os.environ.get('redis_host') or 'localhost'
    redis_port = int(os.environ.get('redis_port') or 6379)
    idle_seconds = float(os.environ.get('idle_seconds') or 1)
    consumer_group = os.environ.get('consumer_group') or 'drainpipe'
    consumer_name = os.environ.get('HOSTNAME') or 'local'
    if 'linuxkit' in consumer_name:
        consumer_name = random.randint(0, 10e10)  # docker for mac

    cache = redis.Redis(redis_host, redis_port)
    drain = StreamDumper(cache, pattern, path_to_csv, consumer_group, consumer_name)

    while True:
        drain.consume_streams()
        time.sleep(idle_seconds)
