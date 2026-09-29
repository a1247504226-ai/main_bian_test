"""
首页页面
"""
from selenium.webdriver.common.by import By
from __runner.settings import key_address_URL
from base.base_page import BasePage, BaseHandle
import time,requests
from page.login_MetaMask import IndexPage_login
from utils import switch_to_new_window, get_url_handles, extract_floats_from_string, DriverUtil


class swapPage(BasePage):
    """合约页面-swap"""

    def __init__(self):
        super().__init__()  # 获取浏览器对象

        self.you_pay_input = (By.XPATH, '/html/body/div/div/div/main/section/div[1]/div[2]/div[1]/div[2]/div[1]/input')  # token0输入框
        self.token0_balances = (By.XPATH, '/html/body/div/div/div/main/section/div[1]/div[2]/div[1]/div[3]/div[2]')  # token1余额
        self.select_token0 = (By.XPATH, '//*[@id=":r1o:"]/div/div[2]/div[1]')  # 可选择的token0默认第一个
        self.selected_token0 = (By.XPATH, '/html/body/div/div/div/main/section/div[1]/div[2]/div[1]/div[2]/div[2]/div')  # 已选择的token0
        self.you_receive_output = (By.XPATH, '/html/body/div/div/div/main/section/div[1]/div[2]/div[2]/div[2]/div[1]/input')  # token1输入框，可得到多少余额
        # self.select_token1 = (By.XPATH, '/html/body/div[2]/div[2]/section/div[2]/div/div[2]/div[2]/div[4]/div/div[1]')  # 可选择的tokens1默认第一个
        self.select_token1 = (By.XPATH, '/html/body/div[2]/div[2]/section/div[2]/div/div[3]/div[2]/div[1]/div/div[1]/div[1]')  # 可选择的tokens1默认第一个
        self.selected_token1 = (By.XPATH, '/html/body/div/div/div/main/section/div[1]/div[2]/div[2]/div[2]/div[2]')  # 选择的token1
        self.token1_balances = (By.XPATH, '/html/body/div/div/div/main/section/div[1]/div[2]/div[2]/div[3]/div[2]/span')  # 选择的token1余额
        self.swap_button = (By.XPATH, '/html/body/div/div/div/main/section/div[1]/div[4]/button')  # swap按钮
        self.swap_accept_button = (By.XPATH, '/html/body/div[2]/div[2]/section/div[2]/div/div[3]/button/span')  # swap确认按钮
        # self.swap_accept_button = (By.XPATH, '//*[@id=":rt:"]/div/div[3]/button/span')  # swap确认按钮
        # self.metamask_accept_button = (By.XPATH, '//*[@id="app-content"]/div/div/div/div/div[3]/button[2]')  # metamask确认按钮
        self.metamask_accept_button2 = (By.XPATH, '//*[@id="app-content"]/div/div/div/div/div[4]/button[2]')  # metamask确认按钮
        self.metamask_accept_button1 = (By.XPATH, '/html/body/div[1]/div/div/div/div/div[3]/div/button[2]')  # metamask确认按钮
        self.close_button  = (By.XPATH, '//*[@id="txn-status-modal-container"]/div[3]/div[2]/button')  # 关闭弹窗

    def find_you_pay_input(self):
        """获取余额定位方法"""
        return self.find_element_func(self.you_pay_input)

    def find_token0_balances(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.token0_balances)

    def find_select_token0(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.select_token0)

    def find_selected_token0(self):
        """获取余额定位方法"""
        return self.find_element_func(self.selected_token0)

    def find_you_receive_output(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.you_receive_output)

    def find_select_token1(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.select_token1)

    def find_selected_token1(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.selected_token1)

    def find_token1_balances(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.token1_balances)
    def find_swap_button(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.swap_button)
    def find_swap_accept_button(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.swap_accept_button)
    def find_metamask_accept_button1(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.metamask_accept_button1)
    def find_metamask_accept_button2(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.metamask_accept_button2)
    def find_close_button(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.close_button)

class swapPageHandle(BaseHandle):
    """提现-操作层"""

    def __init__(self):
        self.swap_page = swapPage()  # 元素定位对象
        self.driver = DriverUtil.get_driver()

    def input_you_pay_input(self,money):
        """待支付点击方法"""
        self.input_text(self.swap_page.find_you_pay_input(),money)

    def text_token0_balances(self):
        """立即支付点击方法"""
        time.sleep(10)
        return self.text_func(self.swap_page.find_token0_balances())

    def click_select_token0(self):
        """立即支付点击方法"""
        self.click_func(self.swap_page.find_select_token0())

    def click_selected_token0(self):
        """立即支付点击方法"""
        self.click_func(self.swap_page.find_selected_token0())

    def text_selected_token0(self):
        """立即支付点击方法"""
        return self.text_func(self.swap_page.find_selected_token0())

    def text_you_receive_output(self):
        """立即支付点击方法"""
        time.sleep(3)
        # return self.driver.find_you_receive_output().get_attribute("value")
        return self.value_func(self.swap_page.find_you_receive_output(),"value")

    def click_select_token1(self):
        """立即支付点击方法"""
        time.sleep(3)
        self.click_func(self.swap_page.find_select_token1())

    def click_selected_token1(self):
        """立即支付点击方法"""
        time.sleep(3)
        self.click_func(self.swap_page.find_selected_token1())

    def text_selected_token1(self):
        """立即支付点击方法"""
        return self.text_func(self.swap_page.find_selected_token1())

    def text_token1_balances(self):
        """立即支付点击方法"""
        return self.text_func(self.swap_page.find_token1_balances())
    def click_swap_button(self):
        """立即支付点击方法"""
        time.sleep(3)
        self.click_func(self.swap_page.find_swap_button())
    def click_swap_accept_button(self):
        """立即支付点击方法"""
        time.sleep(10)
        self.click_func(self.swap_page.find_swap_accept_button())
    def click_metamask_accept_button1(self):
        """立即支付点击方法"""
        time.sleep(5)
        self.click_func(self.swap_page.find_metamask_accept_button1())
    def click_metamask_accept_button2(self):
        """立即支付点击方法"""
        time.sleep(5)
        self.click_func(self.swap_page.find_metamask_accept_button2())

    def click_find_close_button(self):
        """立即支付点击方法"""
        time.sleep(3)
        self.click_func(self.swap_page.find_close_button())

class IndexPage_swap(object):
    """swap交易下单页面"""

    def __init__(self):
        self.driver = DriverUtil.get_driver()
        self.Swap_Page=swapPageHandle()
        self.IndexPage_login = IndexPage_login()

    def login(self,url="swap"):
        self.IndexPage_login.login_MetaMast(key_address_URL)
        time.sleep(3)
        self.IndexPage_login.login_testnet(url,3)

    def swap_input(self,pay_money=0.01):
        get_url_handles()
        self.Swap_Page.click_selected_token1()
        self.Swap_Page.click_select_token1()
        self.Swap_Page.input_you_pay_input(pay_money)

    def get_you_receive_output(self):
        print(self.Swap_Page.text_you_receive_output())
        print(type(self.Swap_Page.text_you_receive_output()))
        text_you_receive_output = float(self.Swap_Page.text_you_receive_output())
        print(type(text_you_receive_output))
        return text_you_receive_output

    def get_token_balances(self):
        token0_balances=extract_floats_from_string(self.Swap_Page.text_token0_balances())[0]
        token1_balances=extract_floats_from_string(self.Swap_Page.text_token1_balances())[0]
        print(type(token0_balances))
        print(type(token1_balances))
        print("token0_balances:"+str(token0_balances))
        print("token1_balances:"+str(token1_balances))
        return token0_balances,token1_balances

    def click_swap(self):
        time.sleep(50)
        self.Swap_Page.click_swap_button()
        for i in range(2):
            try:
                self.Swap_Page.click_swap_accept_button()
                print("点击一次")
            except:
                time.sleep(3)
                print("无需多点击")
        switch_to_new_window(0, 1)
        for i in range(2):
            try:
                try:
                    self.driver.refresh()
                    self.Swap_Page.click_metamask_accept_button1()
                except:
                    self.Swap_Page.click_metamask_accept_button2()
            except:
                print("无需多点击")
        switch_to_new_window(-1)
        self.Swap_Page.click_find_close_button()


if __name__ == '__main__':
    IndexPage_swap().click_swap()