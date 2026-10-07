"""Fixed Windows x64 process inspection. Unknown layouts/permissions fail closed.

PEB offsets are Windows x64 specific; validate pointer width and reject WOW64.
Hold the verified native handle for termination so PID reuse cannot target another process.
"""
import ctypes
from ctypes import wintypes as W
import os
from pathlib import Path
import struct

from remediation.state import RemediationError


def _apis():
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise RemediationError("process_identity_unconfirmed")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    native = ctypes.WinDLL("ntdll")
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.ReadProcessMemory.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
    kernel.GetProcessTimes.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    kernel.IsWow64Process.argtypes = [W.HANDLE, ctypes.POINTER(W.BOOL)]
    kernel.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
    kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    native.NtQueryInformationProcess.argtypes = [W.HANDLE, W.ULONG, ctypes.c_void_p, W.ULONG, ctypes.POINTER(W.ULONG)]
    native.NtQueryInformationProcess.restype = ctypes.c_long
    return kernel, native


class ProcessHandle:
    def __init__(self, pid, terminate=False):
        self.kernel, self.native = _apis()
        self.handle = self.kernel.OpenProcess(0x0400 | 0x0010 | 0x100000 | (1 if terminate else 0), False, pid)
        if not self.handle:
            raise RemediationError("process_identity_unconfirmed")
        self.pid = pid

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.kernel.CloseHandle(self.handle)

    def read(self, address, size):
        data = ctypes.create_string_buffer(size)
        count = ctypes.c_size_t()
        if not self.kernel.ReadProcessMemory(self.handle, address, data, size, ctypes.byref(count)) or count.value != size:
            raise RemediationError("process_identity_unconfirmed")
        return data.raw

    def unicode(self, address):
        data = self.read(address, 16)
        length = int.from_bytes(data[:2], "little")
        pointer = int.from_bytes(data[8:16], "little")
        if not pointer or not 0 < length <= 32768 or length % 2:
            raise RemediationError("process_identity_unconfirmed")
        return self.read(pointer, length).decode("utf-16-le")

    def snapshot(self):
        wow64 = W.BOOL()
        if not self.kernel.IsWow64Process(self.handle, ctypes.byref(wow64)) or wow64.value:
            raise RemediationError("process_identity_unconfirmed")
        basic = (ctypes.c_void_p * 6)()
        size = W.ULONG()
        if self.native.NtQueryInformationProcess(self.handle, 0, basic, ctypes.sizeof(basic), ctypes.byref(size)) != 0:
            raise RemediationError("process_identity_unconfirmed")
        params = int.from_bytes(self.read(basic[1] + 0x20, 8), "little")
        cwd = self.unicode(params + 0x38)
        command = self.unicode(params + 0x70)
        exe = ctypes.create_unicode_buffer(32768)
        length = W.DWORD(len(exe))
        if not self.kernel.QueryFullProcessImageNameW(self.handle, 0, exe, ctypes.byref(length)):
            raise RemediationError("process_identity_unconfirmed")
        times = [ctypes.c_ulonglong() for _ in range(4)]
        if not self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(t) for t in times)):
            raise RemediationError("process_identity_unconfirmed")
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.CommandLineToArgvW.argtypes = [W.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell.CommandLineToArgvW.restype = ctypes.POINTER(W.LPWSTR)
        count = ctypes.c_int()
        argv = shell.CommandLineToArgvW(command, ctypes.byref(count))
        if not argv:
            raise RemediationError("process_identity_unconfirmed")
        try:
            arguments = [argv[i] for i in range(count.value)]
        finally:
            self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
            self.kernel.LocalFree(ctypes.cast(argv, ctypes.c_void_p))
        return {"pid": self.pid, "creation": times[0].value, "cwd": cwd,
                "exe": exe.value, "argv": arguments}

    def stop_verified(self, expected, root):
        snapshot = self.snapshot()
        if snapshot != expected or not belongs_to_demo(snapshot, root):
            raise RemediationError("process_identity_unconfirmed")
        if not self.kernel.TerminateProcess(self.handle, 0):
            raise RemediationError("demo_stop_failed")
        if self.kernel.WaitForSingleObject(self.handle, 5000) != 0:
            raise RemediationError("demo_stop_timeout")


def belongs_to_demo(snapshot, root):
    try:
        root = Path(root).resolve()
        cwd = Path(snapshot["cwd"]).resolve()
        args = snapshot["argv"]
        exe = Path(snapshot["exe"]).resolve()
        return (cwd == root and len(args) == 2
            and exe.name.lower() == "python.exe" and Path(args[0]).resolve() == exe
            and (cwd / args[1]).resolve() == root / "app.py")
    except (KeyError, TypeError, OSError, ValueError):
        return False
