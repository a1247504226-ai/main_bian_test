"""
首页页面
"""
from selenium.webdriver.common.by import By
from __runner.settings import key_address_URL, host
from base.base_page import BasePage, BaseHandle
import time,requests
from page.login_MetaMask import IndexPage_login
from utils import switch_to_new_window, get_url_handles, extract_floats_from_string, DriverUtil

class removeliquidityPage(BasePage):
    """合约页面-swap"""

    def __init__(self):
        super().__init__()  # 获取浏览器对象
        self.remove_button2 = (By.XPATH, '/html/body/div/div/div/main/section/div/div[2]/a[2]/button')  # 移除流动性按钮
        self.remove_button = (By.XPATH, '/html/body/div/div/div/main/section/div/div[2]/a[1]/button')  # 移除流动性按钮
        self.token0_balances = (By.XPATH, '/html/body/div/div/div/main/section/section[1]/div[2]/div[1]/div/div[1]/div/div[1]')  # 选择的token0余额
        self.token1_balances = (By.XPATH, '/html/body/div/div/div/main/section/section[1]/div[2]/div[1]/div/div[2]/div/div[1]')  # 选择的token1余额
        self.remove_againt_button = (By.XPATH, '/html/body/div/div/div/main/section/section/div[2]/button')  # 移除流动性按钮
        self.metamask_button = (By.XPATH, '/html/body/div[1]/div/div/div/div/div[3]/div/button[2]')  # 钱包确认按钮
        self.remove_token0_balances = (By.XPATH, '/html/body/div/div/div/main/section/section/div[2]/div[2]/div[6]/div[1]/div[2]')  # 钱包确认按钮
        self.remove_token1_balances = (By.XPATH, '/html/body/div/div/div/main/section/section/div[2]/div[2]/div[6]/div[2]/div[2]')  # 钱包确认按钮

    def find_remove_button(self):
        """获取余额定位方法"""
        return self.find_element_func(self.remove_button)

    def find_token0_balances(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.token0_balances)

    def find_token1_balances(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.token1_balances)

    def find_remove_againt_button(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.remove_againt_button)

    def find_metamask_button(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.metamask_button)
    def find_remove_token0_balances(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.remove_token0_balances)

    def find_remove_token1_balances(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.remove_token1_balances)

class removeliquidityHandle(BaseHandle):
    """提现-操作层"""

    def __init__(self):
        self.removeliquidity_page = removeliquidityPage()  # 元素定位对象
        self.driver = DriverUtil.get_driver()

    def click_remove_button(self):
        """立即支付点击方法"""
        time.sleep(20)
        self.click_func(self.removeliquidity_page.find_remove_button())

    def text_token0_balances(self):
        """立即支付点击方法"""
        time.sleep(5)
        return self.text_func(self.removeliquidity_page.find_token0_balances())

    def text_token1_balances(self):
        """立即支付点击方法"""
        return self.text_func(self.removeliquidity_page.find_token1_balances())

    def click_remove_againt_button(self):
        """立即支付点击方法"""
        time.sleep(5)
        self.click_func(self.removeliquidity_page.find_remove_againt_button())

    def click_metamask_button(self):
        """立即支付点击方法"""
        time.sleep(10)
        self.click_func(self.removeliquidity_page.find_metamask_button())
        time.sleep(20)

    def text_remove_token0_balances(self):
        """立即支付点击方法"""
        time.sleep(15)
        return self.text_func(self.removeliquidity_page.find_remove_token0_balances())

    def text_remove_token1_balances(self):
        """立即支付点击方法"""
        time.sleep(3)
        return self.text_func(self.removeliquidity_page.find_remove_token1_balances())

class IndexPage_remove_liquid(object):
    """addliquid页面"""

    def __init__(self):
        self.driver = DriverUtil.get_driver()
        self.remove_liquid_Page=removeliquidityHandle()
        self.IndexPage_login = IndexPage_login()

    def remove_liquid_input(self):
        get_url_handles()
        for i in range(2):
            try:
                self.remove_liquid_Page.click_remove_button()
            except:
                self.driver.refresh()
                print("点击click_remove_button失败，重试")
        text_remove_token0_balances = self.remove_liquid_Page.text_remove_token0_balances()
        text_remove_token1_balances = self.remove_liquid_Page.text_remove_token1_balances()
        self.remove_liquid_Page.click_remove_againt_button()
        switch_to_new_window(0, 1)
        self.remove_liquid_Page.click_metamask_button()
        switch_to_new_window(-1)
        return text_remove_token0_balances,text_remove_token1_balances

    def get_token_balances(self,url=""):
        get_url_handles()
        self.driver.get(host+url)
        token0_balances=0
        for i in range(2):
            try:
                token0_balances=extract_floats_from_string(self.remove_liquid_Page.text_token0_balances())[0]
            except:
                self.driver.refresh()
                print("获取余额失败，重试")
        token1_balances=extract_floats_from_string(self.remove_liquid_Page.text_token1_balances())[0]
        print(type(token0_balances))
        print(type(token1_balances))
        print("token0_balances:"+str(token0_balances))
        print("token1_balances:"+str(token1_balances))
        return token0_balances,token1_balances


if __name__ == '__main__':
    IndexPage_remove_liquid().remove_liquid_input()