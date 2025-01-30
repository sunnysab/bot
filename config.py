
import os


__DEFAULT_DEEPSEEK_KEY = 'your-deepseek-key'
__DEFAULT_CHATGLM_KEY = 'your-chatglm-key'
__DEFAULT_OLLAMA_HOST = 'http://localhost:11434'
__WCF_HOST = '192.168.2.105'
__WCF_PORT = 10086

CONFIG = {
    'deepseek-key': os.getenv('DEEPSEEK_KEY', __DEFAULT_DEEPSEEK_KEY),
    'chatglm-key': os.getenv('CHATGLM_KEY', __DEFAULT_CHATGLM_KEY),
    'ollama-host': os.getenv('OLLAMA_HOST', __DEFAULT_OLLAMA_HOST),
    'wcf-host': os.getenv('WCF_HOST', __WCF_HOST),
    'wcf-port': int(os.getenv('WCF_PORT', __WCF_PORT)),
}

