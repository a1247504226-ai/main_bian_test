"""
首页页面
"""
from selenium.webdriver.common.by import By
from __runner.settings import key_address_URL, host
from base.base_page import BasePage, BaseHandle
import time,requests
from page.login_MetaMask import IndexPage_login
from utils import switch_to_new_window, get_url_handles, extract_floats_from_string, DriverUtil


class IncreasliquidPage(BasePage):
    """合约页面-swap"""

    def __init__(self):
        super().__init__()  # 获取浏览器对象
        self.increas_button0 = (By.XPATH, '/html/body/div/div/div/main/section/div/div[2]/a[1]/button')  # 添加流动性按钮
        self.increas_button1 = (By.XPATH, '/html/body/div/div/div/main/section/div/div[2]/a[2]/button')  # 添加流动性按钮
        self.token0_balances = (By.XPATH, '/html/body/div/div/div/main/section/section[1]/div[2]/div[1]/div/div[1]/div/div[1]')  # 选择的token0余额
        self.token1_balances = (By.XPATH, '/html/body/div/div/div/main/section/section[1]/div[2]/div[1]/div/div[2]/div/div[1]')  # 选择的token1余额
        self.input_token_button = (By.XPATH, '/html/body/div/div/div/main/section/section/div[2]/div[4]/div/div/div[2]/div[1]/section[1]/article[1]/div/div/div/div/input')  # 添加流动性输入按钮
        self.increas_button2 = (By.XPATH, '/html/body/div/div/div/main/section/section/div[2]/button')  # 添加流动性按钮
        self.increas_againt_button = (By.XPATH, '/html/body/div[2]/div[2]/section/div[2]/div/button')  # 添加流动性确认按钮
        self.increas_token1_balances = (By.XPATH, '/html/body/div[2]/div[2]/section/div[2]/div/div[2]/div[2]/div[2]')  # 添加流动性确认按钮
        self.metamask_button = (By.XPATH, '/html/body/div[1]/div/div/div/div/div[3]/div/button[2]')  # 钱包确认按钮

    def find_increas_button1(self):
        """获取余额定位方法"""
        return self.find_element_func(self.increas_button1)

    def find_increas_button2(self):
        """获取余额定位方法"""
        return self.find_element_func(self.increas_button2)

    def find_token0_balances(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.token0_balances)

    def find_token1_balances(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.token1_balances)

    def find_increas_againt_button(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.increas_againt_button)

    def find_metamask_button(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.metamask_button)

    def find_input_token_button(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.input_token_button)

    def find_increas_token1_balances(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.increas_token1_balances)

class increasliquidityHandle(BaseHandle):
    """提现-操作层"""

    def __init__(self):
        self.Increasliquidity_page = IncreasliquidPage()  # 元素定位对象
        self.driver = DriverUtil.get_driver()

    def click_increas_button1(self):
        """立即支付点击方法"""
        time.sleep(10)
        self.click_func(self.Increasliquidity_page.find_increas_button1())

    def click_increas_button2(self):
        """立即支付点击方法"""
        time.sleep(3)
        self.click_func(self.Increasliquidity_page.find_increas_button2())

    def text_token0_balances(self):
        """立即支付点击方法"""
        time.sleep(5)
        return self.text_func(self.Increasliquidity_page.find_token0_balances())

    def text_token1_balances(self):
        """立即支付点击方法"""
        return self.text_func(self.Increasliquidity_page.find_token1_balances())

    def click_increas_againt_button(self):
        """立即支付点击方法"""
        time.sleep(1)
        self.click_func(self.Increasliquidity_page.find_increas_againt_button())

    def click_metamask_button(self):
        """立即支付点击方法"""
        time.sleep(10)
        self.click_func(self.Increasliquidity_page.find_metamask_button())
        time.sleep(20)

    def input_token_button(self,money):
        """立即支付点击方法"""
        time.sleep(15)
        self.input_text(self.Increasliquidity_page.find_input_token_button(),money)

    def text_increas_token1_balances(self):
        """立即支付点击方法"""
        time.sleep(5)
        return self.text_func(self.Increasliquidity_page.find_increas_token1_balances())

class IndexPage_increas_liquid(object):
    """addliquid页面"""

    def __init__(self):
        self.driver = DriverUtil.get_driver()
        self.increas_liquid_Page=increasliquidityHandle()
        self.IndexPage_login = IndexPage_login()

    def increas_liquid_input(self,money):
        get_url_handles()
        for i in range(2):
            try:
                self.increas_liquid_Page.click_increas_button1()
            except:
                self.driver.refresh()
                print("点击click_increas_button1失败，重试")
        time.sleep(6)
        self.increas_liquid_Page.input_token_button(money)
        self.increas_liquid_Page.click_increas_button2()
        text_remove_token1_balances = self.increas_liquid_Page.text_increas_token1_balances()
        self.increas_liquid_Page.click_increas_againt_button()
        switch_to_new_window(0, 1)
        self.increas_liquid_Page.click_metamask_button()
        switch_to_new_window(-1)
        return text_remove_token1_balances

    def get_token_balances(self,url=""):
        get_url_handles()
        self.driver.get(host+url)
        token0_balances=0
        for i in range(2):
            try:
                token0_balances=extract_floats_from_string(self.increas_liquid_Page.text_token0_balances())[0]
            except:
                self.driver.refresh()
                print("获取余额失败，重试")
        token1_balances=extract_floats_from_string(self.increas_liquid_Page.text_token1_balances())[0]
        print(type(token0_balances))
        print(type(token1_balances))
        print("token0_balances:"+str(token0_balances))
        print("token1_balances:"+str(token1_balances))
        return token0_balances,token1_balances


if __name__ == '__main__':
    IndexPage_increas_liquid().increas_liquid_input()