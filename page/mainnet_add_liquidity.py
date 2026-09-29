"""
首页页面
"""
from selenium.webdriver.common.by import By
from __runner.settings import key_address_URL, host
from base.base_page import BasePage, BaseHandle
import time,requests
from page.login_MetaMask import IndexPage_login
from utils import switch_to_new_window, get_url_handles, extract_floats_from_string, DriverUtil


class addliquidityPage(BasePage):
    """合约页面-swap"""

    def __init__(self):
        super().__init__()  # 获取浏览器对象

        self.input_token = (By.XPATH, '/html/body/div/div/div/main/div/div/div[5]/div/div/div[1]/section[1]/article[1]/div/div/div/div/input')  # token0输入框
        self.preview_button = (By.XPATH, '/html/body/div/div/div/main/div/div/div[6]/button')  # 添加流动性按钮
        self.token0_balances = (By.XPATH, '/html/body/div/div/div/main/div/div/div[5]/div/div/div[1]/section[1]/article[2]/button/p/span[1]')  # 选择的token0余额
        self.token1_balances = (By.XPATH, '/html/body/div/div/div/main/div/div/div[5]/div/div/div[1]/section[2]/article[2]/button/p/span[1]')  # 选择的token1余额
        self.addliquidity_accept_button = (By.XPATH, '/html/body/div[2]/div[2]/section/div[2]/div/button')  # 添加流动性按钮
        self.metamask_accept_button1 = (By.XPATH, '/html/body/div[1]/div/div/div/div[2]/div[3]/button[2]')  # metamask确认按钮
        self.metamask_accept_button2 = (By.XPATH, '/html/body/div[1]/div/div/div/div/div[3]/div/button[2]')  # metamask确认按钮
        self.see_position_button  = (By.XPATH, '/html/body/div[2]/div[2]/section/div[2]/button')  # 查看position
        self.add_token0_balances  = (By.XPATH, '/html/body/div/div/div/main/section/section[1]/div[2]/div[1]/div/div[1]/div/div[1]')  # 查看position
        self.add_token1_balances  = (By.XPATH, '/html/body/div/div/div/main/section/section[1]/div[2]/div[1]/div/div[2]/div/div[1]')  # 查看position

    def find_input_token(self):
        """获取余额定位方法"""
        return self.find_element_func(self.input_token)

    def find_preview_button(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.preview_button)

    def find_token0_balances(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.token0_balances)

    def find_token1_balances(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.token1_balances)

    def find_addliquidity_accept_button(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.addliquidity_accept_button)
    def find_metamask_accept_button1(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.metamask_accept_button1)
    def find_metamask_accept_button2(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.metamask_accept_button2)
    def find_see_position_button(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.see_position_button)
    def find_add_token0_balances(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.add_token0_balances)
    def find_add_token1_balances(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.add_token1_balances)

class addliquidHandle(BaseHandle):
    """提现-操作层"""

    def __init__(self):
        self.addliquid_page = addliquidityPage()  # 元素定位对象
        self.driver = DriverUtil.get_driver()

    def input_token(self,money):
        """待支付点击方法"""
        time.sleep(5)
        self.input_text(self.addliquid_page.find_input_token(),money)

    def click_preview_button(self):
        """立即支付点击方法"""
        time.sleep(3)
        self.click_func(self.addliquid_page.find_preview_button())

    def text_token0_balances(self):
        """立即支付点击方法"""
        time.sleep(5)
        return self.text_func(self.addliquid_page.find_token0_balances())

    def text_token1_balances(self):
        """立即支付点击方法"""
        return self.text_func(self.addliquid_page.find_token1_balances())

    def click_addliquidity_accept_button(self):
        """立即支付点击方法"""
        time.sleep(5)
        self.click_func(self.addliquid_page.find_addliquidity_accept_button())

    def click_metamask_accept_button1(self):
        """立即支付点击方法"""
        time.sleep(10)
        self.click_func(self.addliquid_page.find_metamask_accept_button1())

    def click_metamask_accept_button2(self):
        """立即支付点击方法"""
        time.sleep(10)
        self.click_func(self.addliquid_page.find_metamask_accept_button2())

    def click_see_position_button(self):
        """立即支付点击方法"""
        time.sleep(20)
        self.click_func(self.addliquid_page.find_see_position_button())

    def text_add_token0_balances(self):
        """立即支付点击方法"""
        time.sleep(5)
        return self.text_func(self.addliquid_page.find_add_token0_balances())
    def text_add_token1_balances(self):
        """立即支付点击方法"""
        time.sleep(1)
        return self.text_func(self.addliquid_page.find_add_token1_balances())

class IndexPage_add_liquid(object):
    """addliquid页面"""

    def __init__(self):
        self.driver = DriverUtil.get_driver()
        self.add_liquid_Page=addliquidHandle()
        self.IndexPage_login = IndexPage_login()

    def add_liquid_input(self,pay_token0=0.01):
        get_url_handles()
        self.add_liquid_Page.input_token(pay_token0)
        self.add_liquid_Page.click_preview_button()
        self.add_liquid_Page.click_addliquidity_accept_button()
        switch_to_new_window(0, 1)
        try:
            self.add_liquid_Page.click_metamask_accept_button1()
        except:
            try:
                self.add_liquid_Page.click_metamask_accept_button2()
            except:
                pass
        switch_to_new_window(-1)
        self.add_liquid_Page.click_see_position_button()
        text_add_token0_balances=extract_floats_from_string(self.add_liquid_Page.text_add_token0_balances())[0]
        text_add_token1_balances=extract_floats_from_string(self.add_liquid_Page.text_add_token1_balances())[0]
        return text_add_token0_balances,text_add_token1_balances

    def get_token_balances(self,url="pool/new-position"):
        localurl=host + url
        self.driver.get(localurl)
        token0_balances=extract_floats_from_string(self.add_liquid_Page.text_token0_balances())[0]
        token1_balances=extract_floats_from_string(self.add_liquid_Page.text_token1_balances())[0]
        print(type(token0_balances))
        print(type(token1_balances))
        print("token0_balances:"+str(token0_balances))
        print("token1_balances:"+str(token1_balances))
        return token0_balances,token1_balances

if __name__ == '__main__':
    IndexPage_add_liquid().add_liquid_inp