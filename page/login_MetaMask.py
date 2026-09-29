"""
首页页面
"""
from selenium.webdriver.common.by import By
from __runner.settings import password, key_address_URL, address_name,host
from base.base_page import BasePage, BaseHandle
import time
from utils import switch_to_new_window, get_url_handles, DriverUtil


class loginMetaMaskPage(BasePage):
    """合约页面-提现-对象库层"""

    def __init__(self):
        super().__init__()  # 获取浏览器对象

        self.metamask_password = (By.ID,'password')  # metamask钱包密码输入框
        self.metamask_pw_button = (By.XPATH, '//*[@id="app-content"]/div/div[2]/div/div/button')  #  metamask钱包密码提交按钮
        self.metamask_address = (By.XPATH, '/html/body/div[1]/div/div[2]/div/div[2]/div/div/button/span[1]/span')  # metamask钱包address
        self.add_butten = (By.XPATH, '//*[@id="app-content"]/div/div[2]/div/div[2]/button/span[1]/span')  # 添加按钮
        self.add_butten2 = (By.CLASS_NAME, 'box--border-width-1')  # 点击按钮
        self.add_butten3 = (By.XPATH, '/html/body/div[3]/div[3]/div/section/div/div[2]/button')  # 点击按钮
        self.input_key_address = (By.XPATH, '//*[@id="private-key-box"]')  # 输入秘钥
        self.add_address_butten = (By.XPATH, '/html/body/div[3]/div[3]/div/section/div/div/div[2]/button[2]')  # 点击添加确定
        self.connect_address1 = (By.XPATH, '/html/body/div/div/div/nav/header/ul[3]/li[3]/div/div/span[2]')  # 点击已连接钱包
        self.connect_address2 = (By.XPATH, '/html/body/div/div/div/nav/header/ul[3]/li[2]/div/div/span[2]')  # 点击已连接钱包
        self.disconnect_address  = (By.XPATH, '/html/body/div[2]/div/div/div[2]/div/div/div/div/div/div[2]/button[2]/div')  # 断开钱包连接
        self.click_metamask_address  = (By.XPATH, '/html/body/div/div/div/nav/header/ul[3]/li/div/button')  # 点击右上角钱包按钮
        self.click_metamask  = (By.XPATH, '/html/body/div[2]/div/div/div[2]/div/div/div/div/div/div[2]/div[2]/div[1]/button/div/div')  # 选择metamask钱包
        self.connect_account_and_accept  = (By.XPATH, '/html/body/div[1]/div/div/div/div[2]/div/div[3]/div/div/button[2]')  # 选择账户连接下一步和确认按钮
        self.begin_send_requests  = (By.XPATH, '/html/body/div/div[1]/div[3]/div/div[4]/div[2]/button/div')  # 点击找回密码
        self.send_requests  = (By.XPATH, '//*[@id="radix-:r6:"]/div[2]/div[3]/button')  # send requests 请求签名
        self.accept_address  = (By.XPATH, '/html/body/div[3]/div[2]/button')  # 同意条款
        self.click_accept_max  = (By.XPATH, '/html/body/div[1]/div/div[3]/div/div[7]/div/div[2]/button')  # 最大支出选择最大
        self.accept_max_outpue  = (By.XPATH, '/html/body/div[1]/div/div[3]/div/div[10]/footer/button[2]')  # 同意最大支出
        self.accept_outpue_butten  = (By.XPATH, '/html/body/div[1]/div/div[3]/div/div[11]/footer/button[2]')  # 确认最大支出
        self.accept_Signature  = (By.XPATH, '//*[@id="app-content"]/div/div[3]/div/div/div[3]/button[2]')  # 同意签名
        self.get_connect_address1  = (By.XPATH, '/html/body/div/div[1]/div[1]/details/summary/span[2]')  # 获取已连接钱包地址信息
        self.get_connect_address2  = (By.XPATH, '/html/body/div/div/div/nav/header/ul[3]/li[3]/div/div/span[2]')  # 获取已连接钱包地址信息
        self.get_except  = (By.XPATH, '/html/body/div[1]/div/div[3]/div/div[3]/div[3]/footer/button[2]')  # 处理掉提现或者充值导致的钱包异常

    def find_metamask_password(self):
        """获取余额定位方法"""
        return self.find_element_func(self.metamask_password)

    def find_metamask_pw_button(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.metamask_pw_button)

    def find_metamask_address(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.metamask_address)

    def find_add_butten(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.add_butten)

    def find_add_butten2(self):
        """点击提现按钮方法"""
        return self.find_elements_func(self.add_butten2)[-1]

    def find_add_butten3(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.add_butten3)

    def find_input_key_address(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.input_key_address)

    def find_add_address_butten(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.add_address_butten)

    def find_connect_address2(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.connect_address2)

    def find_disconnect_address(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.disconnect_address)

    def find_click_metamask_address(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.click_metamask_address)
    def find_click_metamask(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.click_metamask)
    def find_connect_account_and_accept(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.connect_account_and_accept)
    def find_send_requests(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.send_requests)
    def find_accept_address(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.accept_address)
    def find_click_accept_max(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.click_accept_max)
    def find_accept_max_outpue(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.accept_max_outpue)
    def find_accept_outpue_butten(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.accept_outpue_butten)
    def find_accept_Signature(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.accept_Signature)
    def find_connected_address1(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.connect_address2)
    def find_connected_address2(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.get_connect_address2)
    def find_begin_send_requests(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.begin_send_requests)
    def find_get_except(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.get_except)

class loginMetaMaskPageHandle(BaseHandle):
    """提现-操作层"""

    def __init__(self):
        self.my_login_MetaMask_Page = loginMetaMaskPage()  # 元素定位对象

    def send_metamask_password(self,pw):
        """待支付点击方法"""
        self.input_text(self.my_login_MetaMask_Page.find_metamask_password(),pw)

    def click_metamask_pw_button(self):
        """立即支付点击方法"""
        time.sleep(3)
        self.click_func(self.my_login_MetaMask_Page.find_metamask_pw_button())

    def text_metamask_address(self):
        """立即支付点击方法"""
        self.click_func(self.my_login_MetaMask_Page.find_metamask_address())

    def click_add_butten(self):
        """立即支付点击方法"""
        self.click_func(self.my_login_MetaMask_Page.find_add_butten())

    def click_add_butten2(self):
        """立即支付点击方法"""
        self.click_func(self.my_login_MetaMask_Page.find_add_butten2())

    def click_add_butten3(self):
        """立即支付点击方法"""
        self.click_func(self.my_login_MetaMask_Page.find_add_butten3())

    def send_input_key_address(self,key_address):
        """立即支付点击方法"""
        return self.input_text(self.my_login_MetaMask_Page.find_input_key_address(),key_address)

    def click_add_address_butten(self):
        """立即支付点击方法"""
        self.click_func(self.my_login_MetaMask_Page.find_add_address_butten())

    def click_connect_address(self):
        """立即支付点击方法"""
        time.sleep(5)
        self.click_func(self.my_login_MetaMask_Page.find_connect_address2())

    def click_disconnect_address(self):
        """立即支付点击方法"""
        time.sleep(3)
        self.click_func(self.my_login_MetaMask_Page.find_disconnect_address())

    def click_metamask_address(self):
        """立即支付点击方法"""
        self.click_func(self.my_login_MetaMask_Page.find_click_metamask_address())
    def click_metamask(self):
        """立即支付点击方法"""
        self.click_func(self.my_login_MetaMask_Page.find_click_metamask())
    def click_connect_account_and_accept(self):
        """立即支付点击方法"""
        self.click_func(self.my_login_MetaMask_Page.find_connect_account_and_accept())
    def click_send_requests(self):
        """立即支付点击方法"""
        self.click_func(self.my_login_MetaMask_Page.find_send_requests())
    def click_accept_address(self):
        """立即支付点击方法"""
        self.click_func(self.my_login_MetaMask_Page.find_accept_address())
    def click_accept_max(self):
        """立即支付点击方法"""
        self.click_func(self.my_login_MetaMask_Page.find_click_accept_max())
    def click_accept_max_outpue(self):
        """立即支付点击方法"""
        self.click_func(self.my_login_MetaMask_Page.find_accept_max_outpue())
    def click_accept_outpue_butten(self):
        """立即支付点击方法"""
        self.click_func(self.my_login_MetaMask_Page.find_accept_outpue_butten())
    def click_accept_Signature(self):
        """立即支付点击方法"""
        self.click_func(self.my_login_MetaMask_Page.find_accept_Signature())
    def text_connected_address1(self):
        """立即支付点击方法"""
        time.sleep(3)
        return self.text_func(self.my_login_MetaMask_Page.find_connect_address2())
    def text_connected_address2(self):
        """立即支付点击方法"""
        time.sleep(3)
        return self.text_func(self.my_login_MetaMask_Page.find_connected_address2())
    def click_begin_send_requests(self):
        """立即支付点击方法"""
        self.click_func(self.my_login_MetaMask_Page.find_begin_send_requests())
    def click_get_except(self):
        """立即支付点击方法"""
        time.sleep(3)
        self.click_func(self.my_login_MetaMask_Page.find_get_except())

class IndexPage_login(object):
    """首页-对象库"""

    def __init__(self):
        self.login_MetaMask_Page=loginMetaMaskPageHandle()
        self.driver = DriverUtil.get_driver()

    def login_MetaMast(self,key_address_URL):
        self.driver.maximize_window()
        self.driver.get(key_address_URL)
        time.sleep(5)
        self.login_MetaMask_Page.send_metamask_password(password)
        self.login_MetaMask_Page.click_metamask_pw_button()
        for i in range(2):
            try:
                self.driver.refresh()
                self.login_MetaMask_Page.click_get_except()  # 避免提现或者充值影响
            except:
                print("避免提现或者充值影响")

    def login_MetaMast_address(self, key_address):
        try:
            time.sleep(3)
            address_text=self.login_MetaMask_Page.text_metamask_address()
            print(address_text)
            if address_text == address_name:
                return
            else:
                self.login_MetaMask_Page.click_add_butten()
                time.sleep(5)
                # self.login_MetaMask_Page.click_add_butten2()
                # self.login_MetaMask_Page.click_add_butten3()
                # self.login_MetaMask_Page.send_input_key_address(key_address)
                # self.login_MetaMask_Page.click_add_address_butten()
                # time.sleep(5)
                # self.driver.refresh()
        except:
            print("允许的异常错误")

    def login_testnet(self,url="", handle=1, MM=0):
        """登陆testnet方法"""
        switch_to_new_window(-1, key=2)
        URL_local=host + url
        self.driver.get(URL_local)
        # WebDriverWait(driver, 10, 1)
        if handle!= 3 :
            for i in [2]:
                try:
                    time.sleep(5)
                    self.driver.refresh()
                    self.login_MetaMask_Page.click_connect_address()
                    self.login_MetaMask_Page.click_disconnect_address()
                    time.sleep(2)
                except:
                    print("正常没有登录")
        time.sleep(15)
        try:
            self.login_MetaMask_Page.click_metamask_address()
            self.login_MetaMask_Page.click_metamask()
        except:
            print("允许的异常")
        try:
            # 切换到新标签
            switch_to_new_window(MM, 1)
            for i in range(2):
                self.login_MetaMask_Page.click_connect_account_and_accept()
        except:
            print("允许的异常")
        switch_to_new_window(-1)
        time.sleep(3)
        address=''
        for i in range(3):
            if address == '' :
                try:
                    address = self.login_MetaMask_Page.text_connected_address1()
                except:
                    self.driver.refresh()
                    time.sleep(3)
                print('waiting 1 秒')
                print("adress:" + address)
                print("允许的异常")
            else:
                return address
        print("获取address信息超时")

    def other_page_login(self,url=""):
        self.login_MetaMast(key_address_URL)
        time.sleep(3)
        self.login_testnet(url,3)

