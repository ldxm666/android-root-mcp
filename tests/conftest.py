import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.advanced_controller import AdvancedAndroidController

XML = '''<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">
<node text="" class="android.widget.FrameLayout" package="com.example.app" bounds="[0,0][1080,1920]" enabled="true">
<node text="搜索" resource-id="com.example.app:id/query" class="android.widget.EditText" package="com.example.app" clickable="true" focused="true" enabled="true" bounds="[20,20][800,120]"/>
<node text="确定" resource-id="com.example.app:id/ok" class="android.widget.Button" package="com.example.app" clickable="true" enabled="true" bounds="[30,150][300,250]"/>
<node text="确定" resource-id="com.example.app:id/ok2" class="android.widget.Button" package="com.example.app" clickable="true" enabled="true" bounds="[400,150][700,250]"/>
<node text="关闭" resource-id="com.example.app:id/disabled" class="android.widget.Button" package="com.example.app" clickable="true" enabled="false" bounds="[0,300][100,400]"/>
<node text="开关" resource-id="com.example.app:id/toggle" class="android.widget.CheckBox" package="com.example.app" clickable="true" checkable="true" checked="true" enabled="true" bounds="[0,500][100,600]"/>
</node></hierarchy>'''


class FakeU2:
    def __init__(self):
        self.xml = XML
        self.clicks = []
        self.value = "old"
        self.keys = []

    def dump_hierarchy(self, compressed=False):
        return self.xml

    def app_current(self):
        return {"package": "com.example.app", "activity": "com.example.app.MainActivity"}

    def click(self, x, y):
        self.clicks.append((x, y))

    def long_click(self, x, y, duration):
        self.clicks.append((x, y, duration))

    def double_click(self, x, y, duration):
        self.clicks.append((x, y, "double"))

    def __call__(self, **kwargs):
        return self

    def set_text(self, value, timeout=0):
        self.value = value
        return True

    def get_text(self, timeout=0):
        return self.value

    def window_size(self):
        return 1080, 1920


@pytest.fixture
def controller(tmp_path):
    c = AdvancedAndroidController(serial="fake-device", state_dir=tmp_path / "state")
    c._u2_device = FakeU2()
    c.foreground = lambda: c._u2_device.app_current()
    c._device_timeout_available = True
    return c
