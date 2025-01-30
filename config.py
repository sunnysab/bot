
import os


__WHO_AM_I = 'bot'
__DEFAULT_DEEPSEEK_KEY = 'your-deepseek-key'
__DEFAULT_CHATGLM_KEY = 'your-chatglm-key'
__DEFAULT_OLLAMA_HOST = 'http://localhost:11434'

CONFIG = {
    'name': os.getenv('WHO_AM_I', __WHO_AM_I),
    'deepseek-key': os.getenv('DEEPSEEK_KEY', __DEFAULT_DEEPSEEK_KEY),
    'chatglm-key': os.getenv('CHATGLM_KEY', __DEFAULT_CHATGLM_KEY),
    'ollama-host': os.getenv('OLLAMA_HOST', __DEFAULT_OLLAMA_HOST),
}

