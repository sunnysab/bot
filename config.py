import os

__DEFAULT_DEEPSEEK_KEY = 'your-deepseek-key'
__DEFAULT_DEEPSEEK_MODEL = 'deepseek-flash'
__DEFAULT_OLLAMA_HOST = 'http://localhost:11434'
__RELAY_HOST = '127.0.0.1'
__RELAY_PORT = 9099

CONFIG = {
    'deepseek-key': os.getenv('DEEPSEEK_KEY', __DEFAULT_DEEPSEEK_KEY),
    'deepseek-model': os.getenv('DEEPSEEK_MODEL', __DEFAULT_DEEPSEEK_MODEL),
    'ollama-host': os.getenv('OLLAMA_HOST', __DEFAULT_OLLAMA_HOST),
    'relay-host': os.getenv('RELAY_HOST', __RELAY_HOST),
    'relay-port': int(os.getenv('RELAY_PORT', __RELAY_PORT)),
    # 走 Unix domain socket 时填写（如 /tmp/weixin-relay.sock），否则留空走 TCP
    'relay-unix': os.getenv('RELAY_UNIX', ''),
    # 全局最小发送间隔（秒），规避微信风控
    'send-interval': float(os.getenv('SEND_INTERVAL', '1.0')),
}
