import logging

import requests
import sqlparse
import pygments

from requests.packages.urllib3.util.retry import Retry
from pygments.formatters import TerminalTrueColorFormatter
from sqlparse.tokens import Keyword, Newline, Whitespace

from clickhouse_cli import __version__
from clickhouse_cli.clickhouse.definitions import READ_QUERIES, FORMATTABLE_QUERIES
from clickhouse_cli.clickhouse.exceptions import (
    DBException, ConnectionError, TimeoutError
)
from clickhouse_cli.ui.style import CHPygmentsStyle, Echo
from clickhouse_cli.ui.lexer import CHLexer


USER_AGENT = "clickhouse-cli/{0}".format(__version__)

logger = logging.getLogger('main')
echo = Echo()


class Response(object):

    def __init__(self, query, fmt, response='', message='', stream=False):
        self.query = query
        self.message = message
        self.format = fmt
        self.stream = stream
        self.time_elapsed = None
        self.rows = None

        if isinstance(response, requests.Response):
            self.time_elapsed = response.elapsed.total_seconds()

            if stream:
                self.data = response.iter_lines()
                self.rows = None
                return

            self.data = response.text

            lines = self.data.split('\n')

            if self.data == '' or not lines:
                self.rows = 0
            elif fmt.startswith('Pretty'):
                self.rows = sum(1 for line in lines if line.startswith('│'))
            elif fmt in ('TabSeparated', 'TSV', 'CSV'):
                self.rows = len(lines) - 1
            elif fmt in ('TabSeparatedWithNames', 'TSVWithNames', 'CSVWithNames'):
                self.rows = len(lines) - 2
            elif fmt in ('TabSeparatedWithNamesAndTypes', 'TSVWithNamesAndTypes'):
                self.rows = len(lines) - 3
        else:
            self.data = response


class Client(object):

    def __init__(self, url, user, password, database, settings=None, stacktrace=False, timeout=10.0,
                 timeout_retry=0, timeout_retry_delay=0.0):
        self.url = url
        self.user = user
        self.password = password or ''
        self.database = database
        self.settings = settings or {}
        self.cli_settings = {}
        self.stacktrace = stacktrace
        self.timeout = timeout
        self.session = requests.Session()

        retries = Retry(
            connect=timeout_retry,
            # method_whitelist={'GET', 'POST'},  # enabling retries for POST may be a bad idea
            backoff_factor=timeout_retry_delay
        )
        self.session.mount('http://', requests.adapters.HTTPAdapter(max_retries=retries))

    def _query(self, method, query, extra_params, fmt, stream, data=None, compress=False, **kwargs):
        params = {'query': query}
        params.update(extra_params)

        headers = {'Accept-Encoding': 'identity', 'User-Agent': USER_AGENT}
        if compress:
            headers['Content-Encoding'] = 'gzip'

        response = None
        try:
            response = self.session.request(
                method,
                self.url,
                data=data,
                params=params,
                auth=(self.user, self.password),
                stream=stream,
                headers=headers,
                timeout=(self.timeout, None),
                **kwargs
            )
        except requests.exceptions.ConnectTimeout:
            raise TimeoutError
        except requests.exceptions.ConnectionError:
            raise ConnectionError

        if response is not None and response.status_code != 200:
            raise DBException(response, query=query)

        return Response(query, fmt, response, stream=stream)

    def test_query(self):
        params = {'database': self.database}
        params.update(self.settings)
        return self._query(
            'GET',
            'SELECT 1',
            params,
            fmt='Null',
            stream=False,
        )

    def kill_query(self, query_id):
        return self._query(
            'GET',
            'SELECT 1',
            {'replace_running_query': 1, 'query_id': query_id},
            fmt='Null',
            stream=False,
        )

    def query(self, query, data=None, fmt='PrettyCompactMonoBlock',
              stream=False, verbose=False, query_id=None, compress=False, **kwargs):
        query = sqlparse.format(query, strip_comments=True).rstrip(';')
        if verbose and self.cli_settings.get('show_formatted_query'):
            # Highlight & reformat the SQL query
            formatted_query = sqlparse.format(
                query,
                reindent_aligned=True,
                indent_width=2,
                # keyword_case='upper'  # works poorly in a few cases
            )

            if self.cli_settings.get('highlight'):
                print('\n' + pygments.highlight(
                    formatted_query,
                    CHLexer(),
                    TerminalTrueColorFormatter(style=CHPygmentsStyle)
                ))
            else:
                print('\n' + formatted_query)

        # TODO: use sqlparse's parser instead
        query_split = query.split()

        if not query_split:
            return Response(query, fmt)

        # Since sessions aren't supported over HTTP, we have to make some quirks:
        # USE database;
        if query_split[0].upper() == 'USE' and len(query_split) == 2:
            old_database = self.database
            self.database = query_split[1]
            try:
                self.test_query()
            except DBException as e:
                self.database = old_database
                raise e

            return Response(
                query,
                fmt,
                message='Changed the current database to {0}.'.format(
                    self.database
                )
            )

        # SET foo = 100, fizz = 'buzz';
        if query_split[0].upper() == 'SET':
            settings_backup = self.settings.copy()
            for option in ' '.join(query_split[1:]).split(','):
                key, value = option.split('=')
                key, value = key.strip(), value.strip().strip("'")

                self.settings[key] = value
                try:
                    self.test_query()
                except DBException as e:
                    # Roll back all settings, not even keeping the successfully set ones
                    self.settings = settings_backup
                    raise e

            return Response(query, fmt)

        # Set response format
        if query_split[0].upper() in FORMATTABLE_QUERIES and len(query_split) >= 2:
            if query_split[-2].upper() == 'FORMAT':
                fmt = query_split[-1]
            elif query_split[-2].upper() != 'FORMAT':
                if query_split[0].upper() != 'INSERT' or data is not None:

                    if query[-2:] in (r'\g', r'\G'):
                        query = query[:-2] + ' FORMAT Vertical'
                    else:
                        query = query + ' FORMAT {fmt}'.format(fmt=fmt)

        params = {'database': self.database, 'stacktrace': int(self.stacktrace)}
        if query_id:
            params['query_id'] = query_id

        params.update(self.settings)

        # Detect INTO OUTFILE at the end of the query
        t_query = [
            t.value.upper() if t.ttype == Keyword else t.value
            for t in sqlparse.parse(query)[0]
            if t.ttype not in (Whitespace, Newline)
        ]

        try:
            last_tokens = t_query[-5:]
            into_pos = last_tokens.index('INTO')
            has_outfile = into_pos >= 0 and last_tokens.index('OUTFILE') == into_pos + 1

            if has_outfile:
                path = last_tokens[into_pos + 2].strip("'")
                # Remove `INTO OUTFILE '/path/to/file.out'`
                last_tokens.pop(into_pos)
                last_tokens.pop(into_pos)
                last_tokens.pop(into_pos)
                query = ' '.join(t_query[:-5] + last_tokens)
        except ValueError:
            has_outfile = False

        method = 'GET' if query_split[0].upper() in READ_QUERIES else 'POST'
        response = self._query(
            method,
            query,
            params,
            fmt=fmt,
            stream=stream,
            data=data,
            compress=compress,
            **kwargs
        )

        if has_outfile:
            try:
                with open(path, 'wb') as f:
                    if not f:
                        return response

                    if stream:
                        for line in response.iter_lines():
                            f.write(line)
                    else:
                        f.write(response.data.encode())
            except Exception as e:
                echo.warning("Caught an exception when writing to file: {0}".format(e))

        return response
