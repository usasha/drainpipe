import os
import sys
import time

import pandas as pd
import redis

help_message = 'Hello, please pass pattern to match and path to CSV file to dump.'


class StreamDumper:
    def __init__(self, redis, pattern, log_path):
        self.redis = redis
        self.pattern = pattern
        self.log_path = log_path
        self.stream_cursor = dict()

        try:
            with open(self.log_path, 'r') as f:
                self.header = [word for word in f.readline().strip().split(',') if word]
        except FileNotFoundError:
            self.header = []

    @staticmethod
    def find_header(stream_content):
        columns = [column.decode() for column in stream_content[0][1][0][1].keys()]
        if columns:
            return ['stream', 'timestamp'] + columns
        else:
            return []

    def consume_streams(self):
        _, streams = cache.scan(match=self.pattern, count=int(10e10))
        for stream in streams:
            if stream not in self.stream_cursor:
                self.stream_cursor[stream] = self.redis.xrevrange(stream, count=1)[0][0]  # last ID

        result = cache.xread(self.stream_cursor, block=None)
        if not self.header and result:
            self.header = self.find_header(result)
            with open(self.log_path, 'w') as f:
                f.write(','.join(self.header) + '\n')

        for stream, content in result:
            df = pd.DataFrame(content, columns=['timestamp', 'content'])
            df['timestamp'] = df['timestamp'].apply(lambda t: int(t.decode().split('-')[0]) // 1000)
            df['stream'] = stream.decode()

            for column in self.header:
                if column not in ['stream', 'timestamp']:
                    df[column] = df['content'].apply(lambda c: c.get(column.encode()).decode())

            df[self.header].to_csv(self.log_path, mode='a', index=False, header=None)
            self.stream_cursor[stream] = result[-1][1][-1][0]


if __name__ == '__main__':
    try:
        _, pattern, file_name = sys.argv
    except ValueError:
        print(help_message)

    path_to_csv = f'data/{file_name}'
    redis_host = os.environ.get('redis_host') or 'localhost'
    redis_port = int(os.environ.get('redis_port') or 6379)
    idle_seconds = float(os.environ.get('idle_seconds') or 1)

    cache = redis.Redis(redis_host, redis_port)
    drain = StreamDumper(cache, pattern, path_to_csv)

    while True:
        drain.consume_streams()
        time.sleep(idle_seconds)