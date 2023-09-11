import os
import shutil
import sys
import re
import psycopg2
import logging
import time

# {current script}.py -> src -> db-project-manager
db_project_manager_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# setting parent folder for imports
sys.path.insert(1, db_project_manager_path)
# setting parent folder for import config
sys.path.insert(2, os.path.join(db_project_manager_path, "config"))


import  config as cfg

# Parameters
CFG = cfg.Config(os.path.join(db_project_manager_path, "config","config.yaml"))

if __name__ == '__main__':
    print(CFG.gp_connection_string)
    print(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    print(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print(sys.path)