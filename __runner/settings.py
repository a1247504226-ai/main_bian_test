import getpass,os
from __runner.get_metamask_id import get_metamask_id

cur_user = getpass.getuser()
basedir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

host = 'https://app.sonex.so/'
# host = 'https://devnet.sonex.so/'  #devnet
position_url="pool/11846"
# position_url="pool/126"     #devnet
key_address="0195cd3400a8449e422a8a083dc91dd9e661c5113953f51f3e2348ed70fd3531"
# key_address_URL="chrome-extension://{}/home.html#".format(get_metamask_id())
key_address_URL="chrome-extension://nkbihfbeogaeaoehlefnkodbefgpgknn/home.html#"
password="Aa4444634712"
address_name="0x12891...56180"
address="0x12891Ceb915a5139085d9919FBa75A79c4b56180"
localfile=r"C:/jenkins/screenshot"

# #swagger-api
# get_MetaData_URL="api/v1/public/meta/getMetaData"  #获取元信息数据
# post_forcePushPrice="api/v1/public/index/forcePushPrice" #预言机价格推送
# get_ETH_Sepolia="https://cloud.google.com/application/web3/faucet/ethereum/sepolia"
# mail
Mail1_SMTP_Server = "smtp.gmail.com"
Mail1_SMTP_port = 587
Mail1_user = "internal-notice@sx.xyz"
Mail1_password = "zsrhoqkngmdujrck"
Mail_encryption = "SSL"

# FTP
# SFTP

# # processes need to clean
process_map = {
    # browser
    'Chrome': 'chrome.exe',
    'IE': 'iexplore.exe',
    'Edge': 'msedge.exe',
    'FireFox': 'firefox.exe',
    # # excel client
    # 'excel': 'EXCEL.EXE',
    # 'wps': 'wps.exe',
    # 'et': 'et.exe',
    # # folder
    'explorer': 'explorer.exe',
    # others
    'notepad': 'notepad.exe',
}

# chrome config
driver_dir = os.path.join(basedir, 'static/driver/chromedriver.exe')
chrome_user_data = f'--user-data-dir=C:/Users/{cur_user}/AppData/Local/Google/Chrome/User Data'
