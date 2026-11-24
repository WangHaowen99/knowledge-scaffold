"""Administrator-managed upload storage; encrypted connections pinned to each run."""
import json
import os
import re
import uuid
from urllib.parse import urlsplit

from cryptography.fernet import InvalidToken

from .core import Failure, plain_http_url

KEY = 'dataset-settings'
UNCONFIGURED = ('上传文件已保存，但上传数据评测所需的 MySQL 尚未配置。'
                '请管理员在“数据源 → 上传数据存储设置”中配置并测试连接，保存后再启动或恢复评测。')


def dataset(source):
    return source.get('mode') == 'dataset' and source.get('provider') != 'neo4j'


def unavailable(code='dataset_unconfigured', message=UNCONFIGURED):
    return Failure(code, message, 409, 'data_import')


class DatasetSettings:
    def __init__(self, config, store):
        self.config, self.store = config, store

    def default_url(self):
        # Standard compose exposes the evaluation service under this DNS alias.
        # EVAL_PORT is the process/container port, not the host's published port.
        # Compose's internal listener has no TLS. For transport encryption, configure
        # EVAL_NL2SQL_URL to an HTTPS gateway; changing only this scheme breaks queries.
        return self.config.nl2sql_url or 'http://evaluation:{}/evaluation/api/nl2sql'.format(  # NOSONAR(S5332): internal Compose listener; deployment security exception.
            int(os.environ.get('EVAL_PORT', '5180')))

    def deployment(self):
        try:
            sql = json.loads(self.config.sql_config)
            if not isinstance(sql, dict) or not sql.get('host'):
                raise ValueError()
        except (ValueError, TypeError):
            raise unavailable() from None
        return {'sql': sql, 'url': self.default_url(), 'query_auto': True, 'revision': 'deployment'}

    def unpack(self, value):
        try:
            return self.store.decrypt(value)
        except (InvalidToken, ValueError, TypeError):
            raise unavailable('dataset_credentials_unavailable', '上传数据存储配置无法解密，请管理员检查服务加密密钥。') from None

    def effective(self):
        saved = self.store.setting(KEY)
        return self.unpack(saved['sealed']) if saved else self.deployment()

    @staticmethod
    def ready(value):
        try:
            url = urlsplit(value.get('url') or '')
            valid = plain_http_url(url) and url.path.rstrip('/').endswith('/evaluation/api/nl2sql')
            if url.port is not None and not 0 < url.port <= 65535:
                valid = False
        except ValueError:
            valid = False
        if not valid:
            raise unavailable('dataset_query_unconfigured', '上传数据的查询入口尚未配置或格式无效。请管理员在“数据源 → 上传数据存储设置”中填写 Consumer 后端可访问的评测查询入口，路径为 /evaluation/api/nl2sql。')
        return value

    def view(self, admin=False):
        error = None
        try:
            value = self.effective()
        except Failure as exc:
            value, error = {}, exc
        if not error:
            try:
                self.ready(value)
            except Failure as exc:
                error = exc
        result = {'editable': admin, 'configured': error is None,
                  'message': error.message if error else '上传数据存储已配置；新评测会将文件导入独立评测库。'}
        if admin:
            sql = value.get('sql') or {}
            result.update(host=sql.get('host', ''), port=sql.get('port', 3306), user=sql.get('user', ''),
                          password_set=bool(sql.get('password')), query_url=value.get('url') or self.default_url(),
                          query_auto=value.get('query_auto', not bool(value)),
                          revision=value.get('revision'), source='settings' if self.store.setting(KEY) else 'deployment')
        return result

    def candidate(self, body):
        host, user = body['host'].strip(), body['user'].strip()
        if not host or not user or re.search(r'[\s/@?#]', host):
            raise unavailable('dataset_config_invalid', '请填写有效的 MySQL 主机名或 IP、用户名；主机地址不含协议和路径。')
        password = body.get('password') or ''
        if not password:
            try:
                old = self.effective()['sql']
            except Failure:
                old = {}
            if (old.get('host'), old.get('port', 3306), old.get('user')) != (host, body['port'], user):
                raise unavailable('dataset_password_required', '首次配置或修改主机、端口、用户名后，请重新填写 MySQL 密码。')
            password = old.get('password') or ''
            if not password:
                raise unavailable('dataset_password_required', '请填写 MySQL 密码。')
        return self.ready({'sql': {'host': host, 'port': body['port'], 'user': user, 'password': password,
                                  'charset': 'utf8mb4', 'connect_timeout': 10, 'read_timeout': 30, 'write_timeout': 30},
                           'url': (body.get('query_url') or '').strip().rstrip('/') or self.default_url(),
                           'query_auto': not bool((body.get('query_url') or '').strip()),
                           'revision': uuid.uuid4().hex})

    def save(self, body):
        with self.store.lock:
            value = self.candidate(body)
            self.store.put_setting(KEY, {'sealed': self.store.encrypt(value)})
        return self.view(True)

    def bind(self, run_id, value=None):
        """Persist before any MySQL write; a retry or later settings change cannot redirect the run."""
        if not re.fullmatch(r'[a-f0-9]{32}', run_id):
            raise Failure('invalid_run', '运行标识无效', 422)
        key = 'dataset-run:' + run_id
        with self.store.lock:
            saved = self.store.setting(key)
            if saved:
                return self.unpack(saved['sealed'])
            value = self.ready(value or self.effective())
            self.store.put_setting(key, {'sealed': self.store.encrypt(value)})
            return value

    def for_run(self, run_id):
        saved = self.store.setting('dataset-run:' + run_id)
        # Pre-upgrade runs used the deployment connection, never a newly saved page configuration.
        return self.unpack(saved['sealed']) if saved else self.deployment()

    def test(self, body):
        """Explicit admin action: create, write, read and remove a uniquely named synthetic probe database."""
        import pymysql
        value = self.candidate(body)
        schema = 'eval_' + uuid.uuid4().hex
        created = False
        try:
            with pymysql.connect(**value['sql']) as connection:
                with connection.cursor() as cursor:
                    try:
                        cursor.execute('CREATE DATABASE `' + schema + '`')
                        created = True
                        cursor.execute('CREATE TABLE `' + schema + '`.`probe` (value INTEGER)')
                        cursor.execute('INSERT INTO `' + schema + '`.`probe` VALUES (%s)', (1,))
                        cursor.execute('SELECT value FROM `' + schema + '`.`probe`')
                        if cursor.fetchone()[0] != 1:
                            raise ValueError()
                        cursor.execute('DROP TABLE `' + schema + '`.`probe`')
                    finally:
                        if created:
                            try:
                                cursor.execute('DROP DATABASE `' + schema + '`')
                            except Exception:
                                raise unavailable('dataset_test_cleanup_failed', '测试库清理失败，请管理员检查删除权限并清理测试库 ' + schema + '。') from None
        except Failure:
            raise
        except Exception:
            raise unavailable('dataset_connection_failed', 'MySQL 连接或权限测试失败。请检查地址、账号，以及创建评测库、建表、写入、查询和删除权限；测试详情未包含连接凭据。') from None
        return {'success': True, 'message': 'MySQL 创建、写入、查询和清理测试通过。查询入口格式有效；Consumer 到该入口的网络连通性将在评测时检查。'}
