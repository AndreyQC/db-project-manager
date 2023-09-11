import os
import ruamel.yaml as yaml
import logging

class Config(object):

    def __init__(self, yaml_config_file, is_main = True):
        with open(yaml_config_file) as stream:
            try:
                yaml_config = yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                print(exc)

        self.CFG_APPNAME = 'DB_MANAGER_UTILITIES'
        self.CFG_APPSHORTNAME = yaml_config['appShortName']

        assert self.CFG_APPNAME == yaml_config['appName']

        # paths
        self.CFG_WORKING_PATH = yaml_config['paths']['workingpath']
        os.makedirs(self.CFG_WORKING_PATH, exist_ok=True)
        self.CFG_DB_PROJECT_DESTINATION = yaml_config['paths']['destination_path']
        os.makedirs(self.CFG_DB_PROJECT_DESTINATION, exist_ok=True)
        self.LOG_PATH = yaml_config['paths']['log_path']
        os.makedirs(self.LOG_PATH, exist_ok=True)

        # logging configuration
        execution_log_path = os.path.join(self.LOG_PATH, "execution.log")
        if is_main and os.path.exists(execution_log_path):
            os.remove(execution_log_path)

        logging.basicConfig(
            format='%(asctime)s %(levelname)-8s %(message)s',
            level=logging.DEBUG,
            datefmt='%Y-%m-%d %H:%M:%S',
            filename=execution_log_path,
            filemode='a')

        logging_handler = logging.StreamHandler()
        logging_handler.setLevel(logging.INFO)
        logging_handler.setFormatter(logging.Formatter('%(message)s'))
        logging.getLogger().addHandler(logging_handler)


        # Greenplum
        self.GP_DB_PORT = yaml_config['connections']['connection-greenplum']['port']
        self.GP_DB_SSLMODE = yaml_config['connections']['connection-greenplum']['sslmode']
        self.GP_DB_DBNAME = yaml_config['connections']['connection-greenplum']['dbname']
        self.GP_DB_TARGET_SESSION_ATTRS = yaml_config['connections']['connection-greenplum']['target_session_attrs']
        # from env variable
        self.GP_DB_HOST = os.getenv(yaml_config['connections']['connection-greenplum']['host'], "")
        self.GP_DB_USER = os.getenv(yaml_config['connections']['connection-greenplum']['user'], "")
        self.GP_DB_PWD = os.getenv(yaml_config['connections']['connection-greenplum']['password'], "")

        self.GP_DB_CONNECTION_STRING = f"""
            host={self.GP_DB_HOST}
            port={self.GP_DB_PORT}
            sslmode={self.GP_DB_SSLMODE}
            dbname={self.GP_DB_DBNAME}
            user={self.GP_DB_USER}
            password={self.GP_DB_PWD}
            target_session_attrs={self.GP_DB_TARGET_SESSION_ATTRS}
            """
        # clikchouse
        self.CH_DB_HOST = yaml_config['connections']['connection-clickhouse']['host']
        self.CH_DB_PORT = yaml_config['connections']['connection-clickhouse']['port']
        self.CH_DB_USER = os.getenv(yaml_config['connections']['connection-clickhouse']['user'], "")
        self.CH_DB_PASSWORD = os.getenv(yaml_config['connections']['connection-clickhouse']['password'], "")

    @property
    def ch_connection_string(self):
        return f"""
            host={self.CH_DB_HOST}
            port={self.CH_DB_PORT}
            user={self.CH_DB_USER}
            password={self.CH_DB_PASSWORD}
            """
    @property
    def gp_connection_string(self):
        return f"""
            host={self.GP_DB_HOST}
            port={self.GP_DB_PORT}
            sslmode={self.GP_DB_SSLMODE}
            dbname={self.GP_DB_DBNAME}
            user={self.GP_DB_USER}
            connect_timeout = 60000
            password={self.GP_DB_PWD}
            target_session_attrs={self.GP_DB_TARGET_SESSION_ATTRS}
            """

if __name__ == '__main__':
    """if you prepared all env values correctly you will see prepared connection string

    """

    config_folder_path = os.path.dirname(os.path.abspath(__file__))

    config = Config(os.path.join(config_folder_path, r"config.yaml"))

    print(config.gp_connection_string)

    print(config.ch_connection_string)

