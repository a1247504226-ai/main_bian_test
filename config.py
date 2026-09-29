"""
配置文件
"""
import os
import logging.handlers

# 注意: 当前文件的外层文件夹的绝度路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# def config_log():
#     """日志配置方法"""
#     # 初始化日志器
#     #DEBUG(debug)  INFO(info)   WARNING(warning)   ERROR(error)   CRITICAL(critical)
#     logger = logging.getLogger()      #局部新建和获取日志
#     logger.setLevel(level=logging.CRITICAL)  # 修改日志默认输出级别
#
#     # 初始化处理器
#     sh = logging.StreamHandler()  # 控制台
#     th = logging.handlers.TimedRotatingFileHandler(filename=BASE_DIR + '/log/test.log',
#                                                    when='S',
#                                                    interval=5,
#                                                    backupCount=4)

    # import logging
    # logging.basicConfig(level=logging.debug)  # 同样设置全局日志级别为DEBUG   #全局，项目开头
    # # 记录不同级别的日志
    # logging.debug("This is a debug message")
    # logging.info("This is an info message")
    # logging.warning("This is a warning message")
    # logging.error("This is an error message")
    # logging.critical("This is a critical error message")


# # 初始化格式器
#     fmt = '%(asctime)s %(levelname)s [%(name)s] [%(filename)s(%(funcName)s:%(lineno)d)] - %(message)s'
#     formatter = logging.Formatter(fmt)
#
#     # 将格式器添加给处理器
#     sh.setFormatter(formatter)
#     th.setFormatter(formatter)
#
#     # 将处理器添加给日志器
#     logger.addHandler(sh)
#     logger.addHandler(th)
