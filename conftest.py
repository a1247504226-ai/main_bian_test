# # -*- coding: utf-8 -*-
# import pytest ,sys ,os ,pytest ,requests,codecs,time,io
# sys.path.append(os.path.dirname(__file__))
# from utils import DriverUtil
# from _pytest import terminal
#
# # 设置默认编码为 UTF-8
# # sys.stdout = codecs.getwriter("utf-8")(sys.stdout.detach())
# # sys.stdout = original_stdout
# #SLACK_WEBHOOK_URL = ''
#
# @pytest.hookimpl(tryfirst=True, hookwrapper=True)
# def pytest_terminal_summary(terminalreporter, exitstatus, config):
#     """收集测试报告summary,并存入status.txt文件中，供Jenkins调用"""
#     yield
#     # 获取测试结果
#     print("pytest_terminal_summary")
#     passed_num = len([i for i in terminalreporter.stats.get('passed', []) if i.when != 'teardown'])
#     failed_num = len([i for i in terminalreporter.stats.get('failed', []) if i.when != 'teardown'])
#     error_num = len([i for i in terminalreporter.stats.get('error', []) if i.when != 'teardown'])
#     skipped_num = len([i for i in terminalreporter.stats.get('skipped', []) if i.when != 'teardown'])
#     total_num = passed_num + failed_num + error_num + skipped_num
#     test_result = '测试通过' if total_num == passed_num + skipped_num else '测试失败'
#     duration = round((time.time() - terminalreporter._sessionstarttime), 2)
#     successful = len(terminalreporter.stats.get('passed', [])) / (total_num-skipped_num)* 100
#
#     # 定义目录路径
#     directory_path = './report/'
#     # 确保文件所在的目录存在
#     os.makedirs(os.path.dirname(directory_path), exist_ok=True)
#     # 定义文件路径
#     file_path = os.path.join(directory_path, 'status.txt')
#     with open(file_path, 'w', encoding='utf-8') as f:
#         f.write(f'TEST_TOTAL={total_num}\n')
#         f.write(f'TEST_PASSED={passed_num}\n')
#         f.write(f'TEST_FAILED={failed_num}\n')
#         f.write(f'TEST_ERROR={error_num}\n')
#         f.write(f'TEST_SKIPPED={skipped_num}\n')
#         f.write(f'TEST_DURATION={duration}\n')
#         f.write(f'TEST_RESULT={test_result}\n')
#         f.write("SUCCESSFUL=%.2f%%" % successful+"\n")
#         f.write("TOTAL_TIMES=%.2fs" % duration)
#
#     allure_report_url = os.getenv('ALLURE_REPORT_URL', 'http://10.10.20.80:8080/job/QA-web-auto/lastBuild/allure')
#
#     # 创建消息内容
#     message = (f"Sonex主网测试完成啦！！！\n"
#                f"总共用例: {total_num}\n"
#                f"通过用例: {passed_num}\n"
#                f"失败用例: {failed_num}\n"
#                f"跳过用例: {skipped_num}\n\n"
#                f"测试结果: {test_result}\n\n"
#                f"Allure报告: {allure_report_url}")
#
#
#     # 发送消息到Slack
#     try:
#     #     send_slack_message(message)
#         print("推送Slack消息成功！")
#     except Exception as e:
#         print(f"推送Slack消息失败，失败原因: {e}")
#
#
# def send_slack_message(message):
#     headers = {'Content-Type': 'application/json'}
#     payload = {
#         "text": message
#     }
#     response = requests.post(SLACK_WEBHOOK_URL, json=payload, headers=headers)
#     if response.status_code != 200:
#         raise ValueError(f'Request to Slack returned an error {response.status_code}, the response is:\n{response.text}')
#
# def pytest_collection_modifyitems(session, config, items):
#     # print("原始测试用例数量:", len(items))
#     # for item in items:
#     #     print(item.nodeid)  # 打印测试用例的完整路径
#     critical_tests = [item for item in items if item.get_closest_marker("critical")]
#     fast_tests = [item for item in items if item.get_closest_marker("fast")]
#     medium_tests = [item for item in items if item.get_closest_marker("medium")]
#     slow_tests = [item for item in items if item.get_closest_marker("slow")]
#     unmarked_tests = [item for item in items if not any(item.get_closest_marker(marker)
#                                                        for marker in ["critical", "fast", "medium", "slow"])]
#     # print("修改后的测试用例数量:", len(items))
#     # 自定义优先级顺序：critical -> fast -> medium -> slow -> 未标记
#     items[:] = critical_tests + fast_tests + medium_tests + slow_tests + unmarked_tests
#
