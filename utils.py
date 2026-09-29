# """
# 公共方法类
# """
# # -*- coding: utf-8 -*-
# import time,os,pyautogui,re,sys,json,pyautogui,subprocess,shutil
# # 注意: Selenium 框架的异常类需要导入后才可以使用!
# from selenium.common.exceptions import NoSuchElementException
# from selenium.webdriver.common.by import By
# from selenium import webdriver
# from __runner.auto_update_webdriver import check_chrome_driver_update
# from __runner.settings import host, basedir, process_map, key_address, key_address_URL, password, \
#     address
# from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
# from selenium.webdriver.chrome.service import Service
# from webdriver_manager.chrome import ChromeDriverManager
# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
#
# def x6_floats(a):
#     formatted_balances = "{:.6f}".format(a)
#     truncated_balances = float(formatted_balances)
#     return truncated_balances
#
# def are_floats_close(a, b, tolerance=2e-6):
#     """
#     判断两个浮点数是否在给定的误差范围内接近。
#     :param a: 第一个浮点数
#     :param b: 第二个浮点数
#     :param tolerance: 误差范围（默认值为1e-6）
#     :return: 如果a和b在误差范围内接近，则返回True；否则返回False。
#     """
#     return abs(a - b) <= tolerance
#
# def metamask_deposit_timeout(time_out="360"):
#     driver = DriverUtil.get_driver()
#     start_time = time.time()
#     message=''
#     while time.time() < start_time + time_out:
#         if message == "已确认":
#             return message
#         else:
#             driver.refresh()
#             print('waiting 10 秒')
#             time.sleep(10)
#             message = driver.find_element(By.XPATH,'/html/body/div[1]/div/div[3]/div/div/div[1]/div[2]/div/div/div/div/div/div[1]/div/div[2]/div[1]/div[1]/div')
#     return message
#
# def close_all():
#     driver = DriverUtil.get_driver()
#     time.sleep(3)
#     driver.find_element(by=By.XPATH,value='/html/body/div/div[1]/div[3]/div/div[5]/div/div[1]/div/button').click()
#     time.sleep(2)
#     driver.find_element(by=By.XPATH,value='/html/body/div[4]/div[2]/button[2]').click()
#
# # def get_eth_sepolia():
# #     driver = DriverUtil.get_driver()
# #     driver.get(get_ETH_Sepolia)
# #     time.sleep(5)
# #     driver.find_element(by=By.XPATH,value='//*[@id="mat-input-0"]').send_keys(address)
# #     driver.find_element(by=By.XPATH,value='//*[@id="drip"]/cw3-faucet-drip-form/form/button/span[2]').click()
# #     time.sleep(5)
# #     print("24小时一次允许失败且需要登录谷歌账户")
#
# def extract_numbers(s):
#     return [int(num) for num in re.findall(r'\d+', s)]
#
# def extract_floats_from_string(s):
#     try:
#         # 移除字符串中的逗号
#         s = s.replace(',', '')
#     except:
#         print("金额没有逗号，正常")
#     # 使用正则表达式提取所有的浮点数
#     floats = re.findall(r'[-+]?\d*\.\d+|\d+', str(s))
#     # floats = re.findall(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)', str(s))
#     # 将提取到的浮点数从字符串转换为浮点数类型
#     return [float(f) for f in floats]
#
# def get_all_frame():
#     driver = DriverUtil.get_driver()
#     # 查找并获取当前页面中的所有iframe元素
#     iframes = driver.find_elements(By.TAG_NAME,'iframe')
#     print("frame详情%s"%iframes)
#     # 遍历所有的iframe
#     for iframe in iframes:
#         # 切换到每个iframe
#         driver.switch_to.frame(iframe)
#         print(iframe)
#         # 在每个iframe中执行一些操作，比如获取元素
#         # 示例：获取当前iframe中的标题
#         print("当前iframe的标题是:", driver.title)
#         # 切回主页面
#         driver.switch_to.default_content()
#
# def get_url_handles():
#     driver = DriverUtil.get_driver()
#     # 获取当前标签页的URL并打印
#     url = driver.current_url
#     print("当前标签页的URL是:", url)
#
# def switch_to_new_window(number,key=0):
#     """切换新窗口方法"""
#     driver = DriverUtil.get_driver()
#     handles = driver.window_handles
#     print(handles)
#     time.sleep(3)
#     # print("我是句柄0：%s"%handles[1])
#     print("我是句柄1：%s"%handles[-1])
#     if handles[0] == handles[-1] and key==2:
#         time.sleep(3)
#         driver.switch_to.new_window("tab")
#         print("标签页打开成功")
#         handles = driver.window_handles
#     driver.switch_to.window(handles[number])
#     if key == 1 :
#         driver.get(key_address_URL)
#         time.sleep(2)
#         driver.refresh()
#     time.sleep(5)
#
# def clear_cache():
#     try:
#         """清除浏览器缓存"""
#         driver = DriverUtil.get_driver()
#         # # 打开 Chrome 设置页面
#         # driver.get('chrome://settings/clearBrowserData')
#         # 获取所有Cookies
#         cookies = driver.get_cookies()
#         print(f"main: cookies = {cookies}")
#         # 删除所有Cookies
#         driver.delete_all_cookies()
#     except:
#         print("清除缓存失败")
#     # 等待清除完成
#     time.sleep(5)
#     print("浏览器缓存已清除")
#
# def clear_chrome_cache_safely():
#     try:
#         # 获取 Chrome 缓存目录路径
#         appdata_path = os.path.join(os.environ['LOCALAPPDATA'], 'Google', 'Chrome', 'User Data', 'Default', 'Cache')
#         # 检查目录是否存在
#         if os.path.exists(appdata_path):
#             # 删除缓存文件（更安全的方式，不删除整个目录）
#             shutil.rmtree(appdata_path, ignore_errors=True)
#             print(f"已成功清除缓存目录：{appdata_path}")
#         else:
#             print("缓存目录不存在，可能已经被清除。")
#     except Exception as e:
#         print(f"清除缓存时出错: {e}")
#
# # def login_testnet(url, handle=1, MM=0):
# #     """登陆testnet方法"""
# #     driver = DriverUtil.get_driver()
# #     switch_to_new_window(2, key=2)
# #     driver.get(mainnet_host)
# #     time.sleep(5)
# #     # driver.get(url)
# #     # driver.delete_all_cookies()
# #     # WebDriverWait(driver, 10, 1)
# #     try:
# #         driver.find_element(by=By.XPATH, value='/html/body/div/div[5]/div/div/div/a').click()
# #     except:
# #         print("没有主网活动弹窗了")
# #     try:
# #         driver.get(url)
# #         if handle==1:
# #             time.sleep(5)
# #             driver.find_element(by=By.XPATH, value='/html/body/div/div[1]/div[1]/details/summary/span').click()
# #             driver.find_element(by=By.XPATH, value='/html/body/div/div[1]/div[1]/details/ul/a[2]/span').click()
# #             time.sleep(2)
# #     except:
# #         print("没有登录")
# #     finally:
# #         time.sleep(5)
# #         try:
# #             driver.find_element(by=By.XPATH, value='//*[@id="root"]/div[1]/div[1]/button/div[1]').click()
# #             driver.find_element(by=By.XPATH, value='//*[@id="radix-:r0:"]/div[2]/ul/li[5]/div').click()  # 弹窗
# #             # 切换到新标签
# #             switch_to_new_window(MM, 1)
# #             driver.find_element(by=By.XPATH,
# #                                 value='//*[@id="app-content"]/div/div[1]/div/div[3]/div[2]/footer/button[2]').click()  # 弹窗
# #             driver.find_element(by=By.XPATH,
# #                                 value='//*[@id="app-content"]/div/div[1]/div/div[3]/div[2]/footer/button[2]').click()  # 弹窗
# #             switch_to_new_window(0)
# #             driver.find_element(by=By.XPATH, value='//*[@id="radix-:r6:"]/div[2]/button').click()  # 弹窗
# #         except:
# #             print("允许的异常")
# #         # try:
# #         #     driver.find_element(by=By.XPATH, value='/html/body/div/div[1]/div[3]/div/div[4]/div[2]/button/div').click()
# #         #     driver.find_element(by=By.XPATH, value='/html/body/div[3]/div[2]/div[3]/button').click()
# #         # except:
# #         #     print("允许的异常")
# #         finally:
# #             try:
# #                 switch_to_new_window(MM, 1)
# #                 try:
# #                     driver.find_element(by=By.XPATH, value='/html/body/div[3]/div[2]/button').click()  # 同意条款
# #                 except:
# #                     print("钱包第一次登陆才有")
# #                 try:
# #                     time.sleep(5)
# #                     driver.find_element(by=By.XPATH,
# #                                         value="/html/body/div[1]/div/div[3]/div/div[7]/div/div[2]/button").click()  # 同意最大只出
# #                     driver.find_element(by=By.XPATH,
# #                                         value='/html/body/div[1]/div/div[3]/div/div[10]/footer/button[2]').click()  # 同意最大只出
# #                     driver.find_element(by=By.XPATH,
# #                                         value='/html/body/div[1]/div/div[3]/div/div[11]/footer/button[2]').click()  # 确认
# #                 except:
# #                     print("钱包超限确认弹窗")
# #                 switch_to_new_window(2)
# #                 time.sleep(3)
# #                 try:
# #                     driver.find_element(by=By.XPATH, value='/html/body/div/div[1]/div[3]/div/div[4]/div[2]/button/div').click()
# #                 except:
# #                     print("尝试点击找回密码")
# #                 driver.find_element(by=By.XPATH, value='//*[@id="radix-:r6:"]/div[2]/div[3]/button').click()  # 请求签名
# #                 switch_to_new_window(MM, 1)
# #                 start_time = time.time()
# #                 while time.time() < start_time + 35:
# #                     try:
# #                         driver.find_element(by=By.XPATH,
# #                                             value='/html/body/div[1]/div/div[3]/div/div/div[3]/button[2]').click()  # 同意签名
# #                         time.sleep(1)
# #                         driver.get(key_address_URL)
# #                         driver.refresh()
# #                         time.sleep(10)
# #                     except:
# #                         print("多次签名且排除异常确认弹窗")
# #                 get_url_handles()
# #                 switch_to_new_window(2)
# #                 get_url_handles()
# #                 # driver.get(host)
# #                 time.sleep(5)  # 有bug又会几秒刷一次没有
# #             except:
# #                 print("成功连接钱包")
# #             try:
# #                 address = driver.find_element(by=By.XPATH,value='/html/body/div/div[1]/div[1]/details/summary/span[2]').text  #
# #             except:
# #                 address = driver.find_element(by=By.XPATH,value='/html/body/div/div[1]/div[1]/details/summary/span').text  #
# #             print(address)
# #         return address
#
# def login_MetaMast():
#
#     """登陆testnet方法"""
#     driver = DriverUtil.get_driver()
#     driver.get(key_address_URL)
#     print(driver.get(key_address_URL))
#     print("成功")
#     time.sleep(5)
#     try:
#         driver.find_element(by=By.ID, value='password').send_keys(password)
#         driver.find_element(by=By.XPATH, value='//*[@id="app-content"]/div/div[2]/div/div/button').click()
#     except:
#         print("允许的异常")
#         driver.refresh()
#         for i in range(2):
#             try:
#                 time.sleep(2)
#                 driver.find_element(by=By.XPATH,
#                                     value='/html/body/div[1]/div/div[3]/div/div[3]/div[3]/footer/button[2]').click()  # 避免提现或者充值影响
#             except:
#                 print("避免提现或者充值影响")
#     time.sleep(3)
#
# def import_Account():
#     driver = DriverUtil.get_driver()
#     driver.find_element(by=By.XPATH, value='//*[@id="app-content"]/div/div[2]/div/div[2]/button/span[1]/span').click()
#     time.sleep(5)
#     driver.find_elements(by=By.CLASS_NAME, value='box--border-width-1')[-1].click()
#     driver.find_element(by=By.XPATH, value='/html/body/div[3]/div[3]/div/section/div[2]/div[2]/button/span').click()
#     driver.find_element(by=By.XPATH, value='//*[@id="private-key-box"]').send_keys(key_address)
#     driver.find_element(by=By.XPATH, value='/html/body/div[3]/div[3]/div/section/div[2]/div/div[2]/button[2]').click()
#     time.sleep(5)
#     driver.refresh()
#
# def get_message_element(text):
#     """获取特定文本信息对应元素方法"""
#     xpath = '//*[contains(text(),"{}")]'.format(text)
#     driver = DriverUtil.get_driver()
#     try:
#         element = driver.find_element_by_xpath(xpath)
#         return element
#     # except Exception:
#     except NoSuchElementException:
#         return False
#
# def close_user_chrome():
#     # 获取当前用户的 Chrome 进程列表
#     username = os.getlogin()  # 获取当前登录用户名
#     cmd_tasklist = f'tasklist /fi "username eq {username}" /fi "imagename eq chrome.exe"'
#     process_list = os.popen(cmd_tasklist).read()
#
#     # 从输出中提取 PID 列表
#     chrome_pids = []
#     for line in process_list.splitlines():
#         if 'chrome.exe' in line:
#             chrome_pids.append(line.split()[1])  # 提取进程的 PID
#
#     # 杀掉每个 Chrome 进程
#     for pid in chrome_pids:
#         os.popen(f'taskkill /PID {pid} /F')
#     print("已关闭当前用户启动的 Chrome 进程。")
#
# class DriverUtil(object):
#     """浏览器驱动工具类"""
#     _driver = None  # 浏览器对象初始状态
#     _auto_quit = True  # 退出方法附加条件
#
#     @classmethod
#     def get_driver(cls):
#         """获取浏览器驱动方法"""
#         if cls._driver is None:
#             close_user_chrome()
#             time.sleep(10)  # 等待 10 秒
#             # 创建 ChromeOptions 实例
#             options = webdriver.ChromeOptions()
#             # 设置用户数据目录路径
#             user_data_dir = r'{}\AppData\Local\Google\Chrome\User Data'.format(os.path.expanduser('~'))
#             print(f"User Data Dir: {user_data_dir}")  # 打印路径，确保没有错误
#
#             # 去掉 "Chrome 正在受自动化软件控制" 提示
#             options.add_experimental_option("excludeSwitches", ["enable-automation"])
#             options.add_experimental_option('useAutomationExtension', False)
#             # 添加用户数据目录选项
#             options.add_argument(f'--user-data-dir={user_data_dir}')
#             # options.add_argument("--remote-debugging-port=9222")  # 明确指定调试端口
#             capabilities = DesiredCapabilities.CHROME.copy()
#             capabilities['pageLoadStrategy'] = 'none'
#             options.add_argument('--disable-gpu')
#             options.add_argument('--disable-dev-shm-usage')
#             # 清除缓存
#             options.add_argument("--disable-cache")
#             options.add_argument("--disable-application-cache")
#             options.add_argument("--disk-cache-size=0")
#
#             # 使用 webdriver-manager 安装并使用 ChromeDriver
#             service = Service(ChromeDriverManager().install())
#             cls._driver = webdriver.Chrome(service=service, options=options)
#             # 设置超时时间为 20 秒
#             cls._driver.set_page_load_timeout(20)
#             time.sleep(10)  # 等待浏览器完全启动
#             cls._driver.maximize_window()  # 窗口最大化
#             cls._driver.implicitly_wait(20)  # 隐式等待
#
#         return cls._driver
#
#     @classmethod
#     def quit_driver(cls):
#         """退出浏览器驱动方法"""
#         if cls._driver and cls._auto_quit:
#             cls._driver.quit()
#             cls._driver = None
#
#
#     @classmethod
#     def change_quit_status(cls, auto):
#         """
#         修改退出状态方法
#         :param auto: True:打开 False:关闭
#         :return: 无
#         """
#         cls._auto_quit = auto
#
#     @staticmethod
#     def kill_chrome_processes():
#         """
#         查找并终止所有 Chrome 和 WebDriver 进程。
#         """
#         for proc in psutil.process_iter(attrs=['pid', 'name']):
#             try:
#                 # 查找 chrome 浏览器进程和 webdriver 进程
#                 if 'chrome' in proc.info['name'].lower() or 'chromedriver' in proc.info['name'].lower():
#                     print(f"Terminating process {proc.info['name']} with PID {proc.info['pid']}")
#                     proc.terminate()  # 终止进程
#                     proc.wait()  # 等待进程完全结束
#             except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
#                 pass  # 处理没有权限或进程已经结束的情况
#
#
