import os

__DEFAULT_DEEPSEEK_KEY = 'your-deepseek-key'
__DEFAULT_CHATGLM_KEY = 'your-chatglm-key'
__DEFAULT_DOUBAO_MODEL = ''
__DEFAULT_DOUBAO_KEY = ''
__DEFAULT_OLLAMA_HOST = 'http://localhost:11434'
__RELAY_HOST = '127.0.0.1'
__RELAY_PORT = 9099

CONFIG = {
    'deepseek-key': os.getenv('DEEPSEEK_KEY', __DEFAULT_DEEPSEEK_KEY),
    'chatglm-key': os.getenv('CHATGLM_KEY', __DEFAULT_CHATGLM_KEY),
    'doubao-model': os.getenv('DOUBAO_MODEL', __DEFAULT_DOUBAO_MODEL),
    'doubao-key': os.getenv('DOUBAO_KEY', __DEFAULT_DOUBAO_KEY),
    'ollama-host': os.getenv('OLLAMA_HOST', __DEFAULT_OLLAMA_HOST),
    'relay-host': os.getenv('RELAY_HOST', __RELAY_HOST),
    'relay-port': int(os.getenv('RELAY_PORT', __RELAY_PORT)),
    # 走 Unix domain socket 时填写（如 /tmp/weixin-relay.sock），否则留空走 TCP
    'relay-unix': os.getenv('RELAY_UNIX', ''),
}
