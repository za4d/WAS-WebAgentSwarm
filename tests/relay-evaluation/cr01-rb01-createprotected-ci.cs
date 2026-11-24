// CR01/RB01 disposable Windows CI proof ONLY.
// Launch actual Node process SUSPENDED with an explicit process+thread DACL
// at creation, BEFORE any executable instruction or signing secret loads.
// Uses temporary local Windows user credentials supplied in-memory by the
// elevated CI controller. No production service, real keys, or owner PC.
// Windows API: LogonUserW + CreateProcessAsUserW(lpProcessAttributes,
// lpThreadAttributes) + CREATE_SUSPENDED, then ResumeThread.
using System;
using System.Text;
using System.Runtime.InteropServices;
using System.ComponentModel;
using System.Collections.Generic;

public static class RB01CreateProtected {
    [StructLayout(LayoutKind.Sequential)]
    private struct SECURITY_ATTRIBUTES {
        public int nLength;
        public IntPtr lpSecurityDescriptor;
        [MarshalAs(UnmanagedType.Bool)]
        public bool bInheritHandle;
    }

    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    private struct STARTUPINFO {
        public int cb;
        [MarshalAs(UnmanagedType.LPWStr)] public string lpReserved;
        [MarshalAs(UnmanagedType.LPWStr)] public string lpDesktop;
        [MarshalAs(UnmanagedType.LPWStr)] public string lpTitle;
        public int dwX;
        public int dwY;
        public int dwXSize;
        public int dwYSize;
        public int dwXCountChars;
        public int dwYCountChars;
        public int dwFillAttribute;
        public int dwFlags;
        public short wShowWindow;
        public short cbReserved2;
        public IntPtr lpReserved2;
        public IntPtr hStdInput;
        public IntPtr hStdOutput;
        public IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_INFORMATION {
        public IntPtr hProcess;
        public IntPtr hThread;
        public int dwProcessId;
        public int dwThreadId;
    }

    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, EntryPoint="LogonUserW",
        SetLastError=true)]
    private static extern bool LogonUser(
        string username, string domain, string password, int logonType,
        int provider, out IntPtr token);

    [DllImport("advapi32.dll", CharSet=CharSet.Unicode,
        EntryPoint="ConvertStringSecurityDescriptorToSecurityDescriptorW",
        SetLastError=true)]
    private static extern bool ConvertSddl(
        string sddl, uint revision, out IntPtr descriptor, out uint size);

    [DllImport("advapi32.dll", CharSet=CharSet.Unicode,
        EntryPoint="CreateProcessAsUserW", SetLastError=true)]
    private static extern bool CreateProcessAsUser(
        IntPtr token, string applicationName, StringBuilder commandLine,
        ref SECURITY_ATTRIBUTES processAttributes,
        ref SECURITY_ATTRIBUTES threadAttributes,
        [MarshalAs(UnmanagedType.Bool)] bool inheritHandles,
        uint creationFlags, IntPtr environment, string currentDirectory,
        ref STARTUPINFO startup, out PROCESS_INFORMATION processInfo);

    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern uint ResumeThread(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError=true)]
    [return:MarshalAs(UnmanagedType.Bool)]
    private static extern bool TerminateProcess(IntPtr handle, uint exitCode);

    [DllImport("kernel32.dll", SetLastError=true)]
    [return:MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern IntPtr LocalFree(IntPtr mem);

    private struct Spawned {
        public IntPtr Process;
        public IntPtr Thread;
    }
    private static readonly Dictionary<int,Spawned> suspended =
        new Dictionary<int,Spawned>();

    public static int LaunchSuspended(
        string username, string password, string application,
        string scriptPath, string workdir, string trustedUserSid,
        string[] environmentAssignments) {
        if (suspended.Count != 0) throw new InvalidOperationException(
            "One isolated creation-time proof per runner controller");
        if (string.IsNullOrEmpty(password)) throw new ArgumentException("Missing test password");
        IntPtr token=IntPtr.Zero,descriptor=IntPtr.Zero,environment=IntPtr.Zero;
        PROCESS_INFORMATION proc=new PROCESS_INFORMATION();
        try {
            if (!LogonUser(username, ".", password, 2, 0, out token))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "LogonUser failed");
            string sddl="D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;"+trustedUserSid+")";
            uint sdSize;
            if (!ConvertSddl(sddl,1,out descriptor,out sdSize))
                throw new Win32Exception(Marshal.GetLastWin32Error(),"SDDL conversion failed");
            SECURITY_ATTRIBUTES procAttrs=new SECURITY_ATTRIBUTES {
                nLength=Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES)),
                lpSecurityDescriptor=descriptor, bInheritHandle=false
            };
            SECURITY_ATTRIBUTES threadAttrs=procAttrs;
            STARTUPINFO startup=new STARTUPINFO();
            startup.cb=Marshal.SizeOf(typeof(STARTUPINFO));
            // The environment consists only of explicit sandbox/synthetic
            // values; do not inherit the CI admin's broader environment.
            Array.Sort(environmentAssignments,StringComparer.OrdinalIgnoreCase);
            var block=new StringBuilder();
            foreach (string pair in environmentAssignments) {
                if (string.IsNullOrEmpty(pair) || pair.IndexOf('=')<1 ||
                    pair.IndexOf('\0')>=0)
                    throw new ArgumentException("Invalid test environment assignment");
                block.Append(pair).Append('\0');
            }
            block.Append('\0');
            environment=Marshal.StringToHGlobalUni(block.ToString());
            uint flags=0x00000004u /* CREATE_SUSPENDED */
                       |0x00000400u /* CREATE_UNICODE_ENVIRONMENT */
                       |0x08000000u /* CREATE_NO_WINDOW */;
            // lpApplicationName is explicitly pinned; no executable search
            // or unquoted Program.exe path ambiguity.
            StringBuilder cmd=new StringBuilder("\""+application+"\" \""+scriptPath+"\"");
            bool launched=CreateProcessAsUser(
                token,application,cmd,ref procAttrs,ref threadAttrs,
                false,flags,environment,workdir,ref startup,out proc);
            if (!launched)
                throw new Win32Exception(Marshal.GetLastWin32Error(),
                    "CreateProcessAsUserW with creation-time DACL failed");
            if (proc.hProcess==IntPtr.Zero || proc.hThread==IntPtr.Zero)
                throw new InvalidOperationException("Missing process or thread handle");
            suspended[proc.dwProcessId]=new Spawned{
                Process=proc.hProcess,Thread=proc.hThread
            };
            int pid=proc.dwProcessId;
            proc.hProcess=IntPtr.Zero;
            proc.hThread=IntPtr.Zero;
            return pid;
        } finally {
            if (proc.hThread!=IntPtr.Zero) CloseHandle(proc.hThread);
            if (proc.hProcess!=IntPtr.Zero) CloseHandle(proc.hProcess);
            if (environment!=IntPtr.Zero) Marshal.FreeHGlobal(environment);
            if (descriptor!=IntPtr.Zero) LocalFree(descriptor);
            if (token!=IntPtr.Zero) CloseHandle(token);
        }
    }

    public static void Resume(int pid) {
        Spawned h;
        if (!suspended.TryGetValue(pid,out h)) throw new ArgumentException("Not suspended");
        uint result=ResumeThread(h.Thread);
        if (result==0xffffffffu)
            throw new Win32Exception(Marshal.GetLastWin32Error(),"ResumeThread failed");
        CloseHandle(h.Thread);
        CloseHandle(h.Process);
        suspended.Remove(pid);
    }

    public static void StopSuspended(int pid) {
        Spawned h;
        if (!suspended.TryGetValue(pid,out h)) return;
        TerminateProcess(h.Process,1);
        CloseHandle(h.Thread);
        CloseHandle(h.Process);
        suspended.Remove(pid);
    }
}
