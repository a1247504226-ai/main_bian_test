import os, re, sys, winreg, zipfile, time, shutil, socket, requests, subprocess
from pathlib import Path
import urllib.request
import socket
# CookiesName = "chrome_driver_update"
python_root = Path(sys.executable).parent  # python安装目录
socket.setdefaulttimeout(30)
version_re = re.compile(r'^[1-9]\d*\.\d*.\d*')  # 匹配前3位版本信息
chrome_path_driver=subprocess.check_output(['where', 'chrome.exe']).decode('utf-8').strip().replace('\\chrome.exe','')  #chrome的安装目录

def get_chrome_version():
    """通过注册表查询Chrome版本信息: HKEY_CURRENT_USER\SOFTWARE\Google\Chrome\BLBeacon: version"""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'SOFTWARE\Google\Chrome\BLBeacon')
        value = winreg.QueryValueEx(key, 'version')[0]
        return version_re.findall(value)[0]
    except WindowsError as e:
        return '0.0.0'  # 没有安装Chrome浏览器


def get_chrome_driver_version():
    try:
        result = os.popen('chromedriver --version').read()
        version = result.split(' ')[1]
        return '.'.join(version.split('.')[:-1])
    except Exception as e:
        return '0.0.0'  # 没有安装ChromeDriver


def get_latest_chrome_driver(chrome_version):  # 使用淘宝镜像下载安装Chromedriver
    base_url = 'http://npm.taobao.org/mirrors/chromedriver/'  # chromedriver在国内的淘宝镜像网站
    url = f'{base_url}LATEST_RELEASE_{chrome_version}'
    latest_version = requests.get(url).text
    download_url = f'{base_url}{latest_version}/chromedriver_win64.zip'

    # 下载chromedriver zip文件到Python 根目录
    response = requests.get(download_url)
    local_file = python_root / 'chromedriver.zip'
    with open(local_file, 'wb') as zip_file:
        zip_file.write(response.content)

    # 解压缩zip文件到python安装目录
    f = zipfile.ZipFile(local_file, 'r')
    for file in f.namelist():
        f.extract(file, python_root)
    os.popen("")
    f.close()
    local_file.unlink()  # 解压缩完成后删除zip文件


def get_chrome_driver_fromGithub(chrome_version):  # 使用github仓库上的Chromedriver
    # (2024-05-17更新软件逻辑，访问本链接查询最新state版本号系统都超时了，索性这一小节就不执行了也可以)
    down_chrome_version = ""
    try:

        base_url = f'https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_STABLE'
        latest_version = requests.get(base_url).text  # 查询最新的state版本号
        print(f"GitHub库里最新完整版driver = {latest_version}")
        if chrome_version in latest_version:
            down_chrome_version=latest_version
    except BaseException as msg:
        print(f"查询在线最新version-station版本出错{msg}")

    # 遍历所用可能可用的Chrome driver下载链接
    try:
        Look_url = f'https://googlechromelabs.github.io/chrome-for-testing/'
        driver = requests.get(Look_url).text
        Links = []
        codes = re.compile(r'<code>(.*?)</code>', re.S).findall(driver)
        print(f"{codes}")
        for i in codes:
            link = re.compile(r'http(.*?)win64/chromedriver-win64.zip').findall(i)
            Links = Links + link
            # print(f"{Links}")
    except BaseException as msg:
        print(f"查询在线最新version-station版本出错{msg}")
        time.sleep(20)
    else:
        for i in Links:
            download_url = f'https://storage.googleapis.com/chrome-for-testing-public/{down_chrome_version}/win64/chromedriver-win64.zip'
            print(f"正在下载解压,预设定的超时时间为30秒，请耐性等待。\n{download_url}")
            if download_chrome_driver_by_Request(download_url=download_url) == True:
                return True
        print(
            f"本脚本下载失败请人工复制链接地址自行访问该网址下载。",end="\n\n\n")
        time.sleep(100)
        return False

    get_chrome_driver_fromGithub(chrome_version)


def download_chrome_driver_by_Request(download_url):
    # 逐一下载chromedriver zip文件到Python 根目录
    local_file = python_root / 'chromedriver.zip'

    try:
        response = requests.get(download_url, stream=True, allow_redirects=True)
        with open(local_file, 'wb') as zip_file:
            zip_file.write(response.content)
        extract_zip(local_file)
    except BaseException as msg:
        print(f"下载{download_url}出错{msg}")
        return download_chrome_driver_by_urlopen(download_url, local_file)
    else:
        return True


def download_chrome_driver_by_urlopen(download_url, local_file):
    try:
        header = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.84 Safari/537.36 HBPC/12.1.3.306'}
        socket.setdefaulttimeout(30)  # 设置超时时间为20秒
        request = urllib.request.Request(download_url, headers=header)
        response = urllib.request.urlopen(request)
        with open(local_file, 'wb') as zip_file:
            zip_file.write(response.content)
        extract_zip(local_file)
    except BaseException as msg:
        print(f"下载{download_url}出错{msg}")
        return False
    else:
        return True


def extract_zip(local_file):
    try:
        # 解压缩zip文件到python安装目录
        f = zipfile.ZipFile(local_file, 'r')
        f.extract('chromedriver-win64/chromedriver.exe', python_root)
        f.close()
        local_file.unlink()  # 解压缩完成后删除zip文件

        # 从chromedriver-win64目录移动Chromedriver.exe
        try:
            os.remove(f"{python_root}\chromedriver.exe")
        except:
            print("删除失败")
        finally:
            # shutil.copyfile(f"{python_root}\chromedriver-win64\chromedriver.exe", chrome_path_driver)
            shutil.move(f"{python_root}\chromedriver-win64\chromedriver.exe", python_root)
        print(python_root)
        # shutil.copyfile(f"{python_root}/chromedriver-win64/chromedriver.exe", f"{python_root}/chromedriver.exe")

    except BaseException as msg:
        print(f"解压缩本地文件时遇到错误{msg}")
        return False
    else:
        return True


def check_chrome_driver_update():
    chrome_version = get_chrome_version()
    driver_version = get_chrome_driver_version()
    print(f'chrome_version = {chrome_version},driver_version = {driver_version}')
    if chrome_version == driver_version:
        print('No need to update')
    else:
        try:
            get_chrome_driver_fromGithub(chrome_version)
        except Exception as e:
            print(f'GitHub仓库Fail to update: {e}')
        else:
            print("Success to download chrome_driver.")


if __name__ == '__main__':
    check_chrome_driver_update()
