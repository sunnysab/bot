import argparse
import ctypes
import os.path


class Wcf:
    DEFAULT_SDK_PATH = 'binary/sdk.dll'

    def __init__(self):
        pass

    def load(self, debug: bool = False, port: int = 10086):
        if not os.path.exists(self.DEFAULT_SDK_PATH):
            print('SDK 不存在！')

        self.sdk = ctypes.cdll.LoadLibrary(self.DEFAULT_SDK_PATH)
        # 初始化 SDK. 出现错误时，SDK 会调用 MessageBox 弹窗提示错误信息并返回 -1
        if self.sdk.WxInitSDK(debug, port) != 0:
            exit(-1)

    def cleanup(self):
        if self.sdk.WxDestroySDK() != 0:
            print('退出失败！')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Wcf SDK Loader')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    parser.add_argument('--port', type=int, default=10086, help='Port number to use')
    args = parser.parse_args()

    while True:
        wcf = Wcf()
        wcf.load(debug=args.debug, port=args.port)

        key = input('Press Enter to exit...')
        wcf.cleanup()
        if key != 'r':
            break
