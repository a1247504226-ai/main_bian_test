import pytest,os,yaml,json,logging,sys
from parameterized import parameterized

def find_data_yaml_in_folder(current_script_path,order):
    folder = current_script_path
    logging.info(folder)
    print("我是运行路径：%s"%folder)
    paths = os.listdir(folder)
    # 初始化 name 变量
    name = None
    for i in paths:
        if "test_" in i:
            name = i.replace('test_', '').replace('.py', '')
    print("我是路径:%s" %name)
    data_folder = os.path.join(folder, 'data')
    print(os.path.isdir(data_folder))
    if os.path.isdir(data_folder):
        result = os.path.join(data_folder, name + order + '.json')
        print("result:%s" % result)
        if os.path.exists(result):
            return result
    return None

def build_login_data(current_script_path,order=""):
    """登录测试数据构造方法"""
    data_dir = find_data_yaml_in_folder(current_script_path,order)
    if data_dir is None:
        raise FileNotFoundError("Data file not found.")
    print("我是测试路径：%s" % data_dir)
    with open(data_dir, encoding='utf-8') as f:
        data = json.load(f)
        data_list = []
        # 遍历数据，提取所有字段
        for item in data.values():
            # 将 item 中的所有字段作为元组返回
            data_list.append(tuple(item.values()))
        print(data_list)
        logging.info(data_list)
        return data_list

#
# def find_data_yaml_in_folder():
#     folder = os.getcwd()
#     print("我是运行路径：%s"%folder)
#     paths = os.listdir(folder)
#     for i in paths:
#         if "test_" in i:
#             name = i.replace('test_', '').replace('.py', '')
#     print("我是路径:%s" %name)
#     data_folder = os.path.join(folder, 'data')
#     if not os.path.isdir(data_folder):
#         raise FileNotFoundError("Data folder does not exist.")
#     result = os.path.join(data_folder, f"{name}.yaml")
#     if os.path.exists(result):
#         return result
#     # 尝试 JSON 文件
#     result_json = os.path.join(data_folder, f"{name}.json")
#     if os.path.exists(result_json):
#         return result_json
#     raise FileNotFoundError(f"Data file '{name}.yaml' or '{name}.json' not found in 'data' folder.")
#
#
# def build_login_data():
#     """登录测试数据构造方法"""
#     data_dir = find_data_yaml_in_folder()
#     if data_dir is None:
#         raise FileNotFoundError("Data file not found.")
#     print("我是测试路径：%s" % data_dir)
#     # 选择文件扩展名
#     file_extension = os.path.splitext(data_dir)[1].lower()
#     # 读取文件并解析内容
#     if file_extension == '.yaml':
#         with open(data_dir, encoding='utf-8') as f:
#             data = yaml.safe_load(f)
#     elif file_extension == '.json':
#         with open(data_dir, encoding='utf-8') as f:
#             data = json.load(f)
#     else:
#         raise ValueError("Unsupported file format. Only .json and .yaml are supported.")
#     data_list = []
#     # 遍历数据，提取所有字段
#     for item in data.values():
#         data_list.append(tuple(item.values()))
#     print(data_list)
#     logging.info(data_list)
#     return data_list
