import os


__DEFAULT_DEEPSEEK_KEY = 'your-deepseek-key'
__DEFAULT_CHATGLM_KEY = 'your-chatglm-key'
__DEFAULT_OLLAMA_HOST = 'http://localhost:11434'
__WCF_HOST = '192.168.2.105'
__WCF_PORT = 5000
__REMOTE_STORAGE_PATH = r'C:\Users\sunnysab\Desktop\v39\static'
__REMOTE_SERVER_PREFIX = 'http://192.168.2.105:5002/static/'

CONFIG = {
    'deepseek-key': os.getenv('DEEPSEEK_KEY', __DEFAULT_DEEPSEEK_KEY),
    'chatglm-key': os.getenv('CHATGLM_KEY', __DEFAULT_CHATGLM_KEY),
    'ollama-host': os.getenv('OLLAMA_HOST', __DEFAULT_OLLAMA_HOST),
    'wcf-host': os.getenv('WCF_HOST', __WCF_HOST),
    'wcf-port': int(os.getenv('WCF_PORT', __WCF_PORT)),
    'remote-storage-path': os.getenv('REMOTE_STORAGE_PATH', __REMOTE_STORAGE_PATH),
    'remote-server-prefix': os.getenv('REMOTE_SERVER_PREFIX', __REMOTE_SERVER_PREFIX),
}
