import os,json

#加载metamask插件的id
def get_chrome_extension_id_by_default_title(chrome_extensions_path, target_title):
    for extension_id in os.listdir(chrome_extensions_path):
        extension_dir = os.path.join(chrome_extensions_path, extension_id)
        if os.path.isdir(extension_dir):
            for version in os.listdir(extension_dir):
                manifest_path = os.path.join(extension_dir, version, 'manifest.json')
                if os.path.isfile(manifest_path):
                    with open(manifest_path, 'r', encoding='utf-8') as f:
                        manifest = json.load(f)
                        if 'action' in manifest and 'default_title' in manifest['action']:
                            if manifest['action']['default_title'] == target_title:
                                return extension_id
    return None

def get_metamask_id():
    # 根据操作系统设置Chrome扩展程序目录路径
    if os.name == 'nt':  # Windows
        chrome_extensions_path = os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\User Data\Default\Extensions')
    elif os.name == 'posix':
        chrome_extensions_path = os.path.expanduser('~/Library/Application Support/Google/Chrome/Default/Extensions')
        # 如果在Linux上
        if not os.path.exists(chrome_extensions_path):
            chrome_extensions_path = os.path.expanduser('~/.config/google-chrome/Default/Extensions')
    else:
        raise Exception("Unsupported operating system")
    # 指定目标插件名称
    target_name = "MetaMask"
    # 获取指定名称插件的ID
    extension_id = get_chrome_extension_id_by_default_title(chrome_extensions_path, target_name)
    if extension_id:
        print(f"Chrome Extension ID for '{target_name}': {extension_id}")
        return extension_id
    else:
        raise (f"No extension found with  '{target_name}'")