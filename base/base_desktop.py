import pyautogui
import subprocess

def run_application(path: str, params: list) -> int:
    """Run application with arguments.  Wait for application to exit or
    timeout, then return the return code attribute.

    Args:
        path (str): Application path
        params (list): Application parameters

    Returns:
        class 'subprocess.CompletedProcess'
        
    Example:

        .. code-block:: python
        
        rst = run_application('powershell.exe', ['dir; Exit'] )
        print("rst={0}, type(rst)={1}".format(rst, type(rst)))
        #rst=CompletedProcess(args=['powershell.exe', 'dir; Exit'], returncode=0), type(rst)=<class 'subprocess.CompletedProcess'>
        assert(rst.returncode == 0)
        
    """
    return subprocess.run([path] + list(params))

def run_application_asyc(path: str, params: list):
    """Asynchronously Run application with arguments. Do not wait for application to exit or
    timeout.

    Args:
        path (str): Application path
        params (list): Application parameters

    Returns:
        Object of subprocess.Popen
        
    Example:

        .. code-block:: python
        
        process1 = run_application_asyc('C:\Windows\System32\mspaint.exe', [])
        print("process1={0}".format(process1))
        process1.terminate()
        
    """
    return subprocess.Popen([path, *params])


def take_fullscreenshot(save_path="screenshot.png",):
    """Take screenshot for the current screen

    Args:
        save_path (str, optional): Screenshot save path. Defaults to "screenshot.png".

    Example:

        .. code-block:: python
        
        take_fullscreenshot()
        take_fullscreenshot('a.jpg')
        
    """
    image = pyautogui.screenshot()
    image.save(save_path)


def bring_app_to_foreground_withwindowtitle(title:str, maximize: bool=False):
    """Find the application with given window title, and bring it to foreground.
    
    Args:
        title (str): Title of the window you want to bring to foreground.
        maximize (bool): True will maximize the windows before bring it to foreground. Default False.
        
    Example:
        process1 = run_application_asyc('C:\Windows\System32\mspaint.exe', [])
        process2 = run_application_asyc("C:\Program Files\Windows NT\Accessories\wordpad.exe", [])
        time.sleep(0.5)
        bring_app_to_foreground_withwindowtitle('文档 - 写字板')

    """
    if (maximize == True):
        pyautogui.getWindowsWithTitle(title)[0].maximize()
        
    pyautogui.getWindowsWithTitle(title)[0].activate()


def get_screen_size() ->tuple:
    """Get your screen resolution in x, y format: (1920, 1080)"""
    # 获取分辨率
    return pyautogui.size()


def mouse_moveTo(x: int, y: int, duration: float = 0.0) -> None:
    """Moves your mouse pointer from it’s current location to x, y coordinate, and takes time as specified by duration argument to do so. 
    鼠标移动（绝对位置）
    Args:
        x (int):  x coordinate
        y (int):  y coordinate
        duration (float): duration of num_seconds to do movement.
    """
    pyautogui.moveTo(x, y, duration)
    
def mouse_moveRel(xOffset: int, yOffset: int, duration: float = 0.0) -> None:
    """Move mouse pointer at (xOffset, yOffset) relative to its original position. 
    If duration is 0 or unspecified, movement is immediate.
    鼠标移动（相对位置）
    Args:
        xOffset (int): x offset relative to its original position. 
        yOffset (int): y offset relative to its original position. 
        duration (float): num_seconds to do movement.
    """
    pyautogui.moveRel(xOffset, yOffset, duration)

def mouse_postion() -> tuple:
    """Return coordinates where your mouse was residing at the time of executing the program. 
    获取鼠标位置
    """
    return pyautogui.position()

def mouse_click(x: int, y: int, clicks: int=1, interval: float=0.0, button: str='left') -> None:
    """Performs a typical mouse click at the location.
    点击
    Args:
        x (int): moveToX
        y (int): moveToY
        clicks (int): num of clicks
        interval (float): secs between clicks
        button (str): 'left', 'middle', 'right'. Defaut is 'left'.
        
    Example:
        mouse_click(100, 100)
    """
    pyautogui.click(x, y, clicks, interval, button)

def mouse_rightClick(x:int, y: int) -> None:
    """Performs a typical mouse right click at the location.
    点击
    Args:
        x (int): moveToX
        y (int): moveToY
        
    Example:
        mouse_rightClick(100, 100)
    """
    mouse_click(x, y, button='right')
    
def mouse_middleClick(x:int, y: int) -> None:
    """Performs a typical mouse right click at the location.
    点击
    Args:
        x (int): moveToX
        y (int): moveToY
        
    Example:
        mouse_middleClick(100, 100)
    """
    mouse_click(x, y, button='middle')

def mouse_doubleClick(x:int, y: int) -> None:
    """Performs a typical mouse double click at the location.
    
    Args:
        x (int): moveToX
        y (int): moveToY
        
    Example:
        mouse_doubleClick(100, 100)
    """
    mouse_click(x, y, clicks=2, interval=0.5, button='left')
    
def mouse_dragTo(x: int, y: int, duration: float = 0.0) -> None:
    """Drag mouse to (x,y).
    
    Args:
        x (int): x coordinate
        y (int): y coordinate
        duration (float): number of seconds to do dragging.
    """
    pyautogui.dragTo(x, y, duration)

def mouse_dragRel(xOffset: int, yOffset: int, duration: float = 0.0) -> None:
    """Drag mouse relative to its current position.
    
    Args:
        xOffset (int): x offset relative to its original position. 
        yOffset (int): y offset relative to its original position. 
        duration (float): number of seconds to do dragging.
    """
    pyautogui.dragRel(xOffset, yOffset, duration)


def typewrite(message, interval=0.0) -> None:
    """Performs a keyboard key press down, followed by a release, for each of
    the characters in message.
    键盘输入
    The message argument can also be list of strings, in which case any valid
    keyboard name can be used.

    Args:
      message (str, lista
      c): The number of seconds in between each press.
        0.0 by default, for no pause in between presses.
    Returns:
      None
      
    Examples:
        keyboard_typewrite('Hello world!\n', interval=0.1)
        keyboard_typewrite(['a', 'b', 'c', 'left', 'backspace', 'enter', 'f1'], interval=0.1)
    """
    pyautogui.typewrite(message, interval)

def keyboard_hotkey(*args, **kwargs) -> None :
# """Performs key down presses on the arguments passed in order, then performs
#     key releases in reverse order.
#     The effect is that calling hotkey('ctrl', 'shift', 'c') would perform a
#     "Ctrl-Shift-C" hotkey/keyboard shortcut press.
#     Args:
#       key(s) (str): The series of keys to press, in order. This can also be a
#         list of key strings to press.
#       interval (float, optional): The number of seconds in between each press.
#         0.0 by default, for no pause in between presses.
#     Returns:
#       None
#
#     Examples:
#         keyboard_hotkey('ctrl', 'c')  # ctrl-c to copy
#         keyboard_hotkey('ctrl', 'v')  # ctrl-v to paste
#     """
    pyautogui.hotkey(args, kwargs)

def keyboard_keyDown(key: str) -> None:
    """Performs a keyboard key press without the release. This will put that
    key in a held down state.
      NoneNone

    Returns:
      None
      
    Examples:
        keyboard_keyDown('c')
    """
    pyautogui.keyDown(key)

def keyboard_keyUp(key: str) -> None:
    """Performs a keyboard key release (without the press down beforehand).
    模拟按钮释放
    Args:
      key (str): The key to be released up. The valid names are listed in
      KEYBOARD_KEYS.

    Returns:
      None
      
    Examples:
        keyboard_keyUp('c')
    """
    pyautogui.keyUp(key)
        