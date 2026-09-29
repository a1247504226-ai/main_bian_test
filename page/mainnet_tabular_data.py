"""
首页页面
"""
from selenium.webdriver.common.by import By
from __runner.settings import key_address_URL, host
from base.base_page import BasePage, BaseHandle
import time,requests
from page.login_MetaMask import IndexPage_login
from utils import switch_to_new_window, get_url_handles, extract_floats_from_string, DriverUtil


class datatvlPage(BasePage):
    """合约页面-swap"""

    def __init__(self):
        super().__init__()  # 获取浏览器对象

        self.data_tvl = (By.XPATH, '/html/body/div/div/div/main/div/div[1]/div[1]/div/div[1]/section[2]')  # 图表TVL数据
        self.data_volume = (By.XPATH, '/html/body/div/div/div/main/div/div[1]/div[1]/div/div[2]/section[1]')  # 图表volume

    def find_data_tvl(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.data_tvl)
    def find_data_volume(self):
        """点击提现按钮方法"""
        return self.find_element_func(self.data_volume)

class datatvlHandle(BaseHandle):
    """提现-操作层"""
    def __init__(self):
        self.addliquid_page = datatvlPage()  # 元素定位对象
        self.driver = DriverUtil.get_driver()

    def text_data_tvl(self):
        """立即支付点击方法"""
        time.sleep(5)
        return self.text_func(self.addliquid_page.find_data_tvl())
    def text_data_volume(self):
        """立即支付点击方法"""
        time.sleep(5)
        return self.text_func(self.addliquid_page.find_data_volume())

class IndexPage_tabular_data(object):
    """addliquid页面"""

    def __init__(self):
        self.driver = DriverUtil.get_driver()
        self.add_liquid_Page=datatvlHandle()
        self.IndexPage_login = IndexPage_login()

    def data_tvl_volume(self):
        text_text_data_tvl=extract_floats_from_string(self.add_liquid_Page.text_data_tvl())[0]
        text_text_data_volume=extract_floats_from_string(self.add_liquid_Page.text_data_volume())[0]
        print("我是tvl数据："+str(text_text_data_tvl))
        print("我是volume数据："+str(text_text_data_volume))
        return text_text_data_tvl,text_text_data_volume


if __name__ == '__main__':
    IndexPage_tabular_data().data_tvl_volume()