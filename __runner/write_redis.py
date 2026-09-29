# import os, sys
# sys.path.append(os.path.abspath(os.path.dirname('..')))
#
# import redis, wmi, socket
# from __runner.mrpa import runner
#
# r = redis.Redis(host='172.25.128.163', port=6379, db=3)
#
#
# class MachineInfo:
#     def __init__(self):
#         self.wmi_source = wmi.WMI()
#
#     def get_host(self):
#         # for address in self.wmi_source.Win32_NetworkAdapterConfiguration(ServiceName="e1dexpress"):
#         # return address.IPAddress[0]
#         try:
#             s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
#             s.connect(('8.8.8.8', 80))
#             ip = s.getsockname()[0]
#         finally:
#             s.close()
#         return ip
#
#     def get_sys_info(self):
#         for wos in self.wmi_source.Win32_OperatingSystem():
#             if 'windows 7' in wos.Caption.lower():
#                 caption = wos.Caption + 'SP1'
#                 sys_info = [caption, wos.OSArchitecture]
#             else:
#                 sys_info = [wos.Caption, wos.OSArchitecture]
#             return ' '.join(sys_info)
#
#     def get_cpu(self):
#         for processor in self.wmi_source.Win32_Processor():
#             cpu = [str(processor.NumberOfCores) + 'Cores', processor.Name, processor.Caption]
#             return ','.join(cpu)
#
#     def get_memory(self):
#         for cs in self.wmi_source.Win32_ComputerSystem():
#             return str(round(int(cs.TotalPhysicalMemory) / 1024 ** 3)) + 'GB'
#
#     def get_windows_spec(self):
#         pass
#
# machine = MachineInfo()
#
# def get_machine_info():
#     host_ip = machine.get_host()
#     if host_ip:
#         sys_info = machine.get_sys_info()
#         cpu = machine.get_cpu()
#         memory = machine.get_memory()
#         machine_info = {host_ip: {'sys_info': sys_info, 'cpu': cpu, 'memory': memory}}
#         return machine_info
#     else:
#         raise Exception('get host_ip ERROR')
#
#
# def get_executor():
#     executor = dict()
#     executor['version'] = runner.version
#     executor['sub_version'] = runner.internalVersion
#     return executor
#
#
# def get_redis_mapping(kwargs):
#     mapping = get_machine_info()
#     for k, v in mapping.items():
#         if kwargs:
#             v.update(kwargs)
#             return mapping
#         else:
#             return
#
#
# def write_redis(mapping):
#     if mapping:
#         for k, v in mapping.items():
#             r.hset(k, mapping=v)
#     else:
#         raise ValueError('write machine info to redis ERROR')
#
# def main():
#     version = get_executor()
#     redis_map = get_redis_mapping(version)
#     print(redis_map)
#     write_redis(redis_map)
#     print('write machine info to redis Done !!! ')
#
#
# if __name__ == "__main__":
#     main()
