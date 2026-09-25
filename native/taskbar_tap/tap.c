/*
 * assistant_tap.dll —— 桌面助理的"透明任务栏"组件（可选功能，默认关闭）。
 *
 * 做法（和 TranslucentTB / Windhawk 相同的公开机制，代码从零写）：
 *   助理用 Windows.UI.Xaml.dll 导出的 InitializeXamlDiagnosticsEx 把本 DLL 加载进 explorer，
 *   系统创建我们的 IObjectWithSite 对象并把 IXamlDiagnostics 交给 SetSite；
 *   我们订阅可视树（AdviseVisualTreeChange，初次订阅会把现有元素全部回调一遍），
 *   找到 Taskbar.TaskbarBackground 下面名为 BackgroundFill / BackgroundStroke 的 Rectangle，
 *   把 Fill 换成透明画刷；还原 = ClearProperty（去掉本地值，回到系统样式）。
 * 模式由初始化数据传入："probe"（只记日志，不改任何东西）/"clear"（透明）/"restore"（还原）。
 * 原则：任何一步失败都只写日志、直接返回，绝不抛异常、不访问空指针——出错的代价是 explorer 崩溃。
 * 日志：%TEMP%\assistant_tap.log
 * 构建：python tools/build_tap.py（Zig 自带的 mingw 头文件里有 xamlom.h）
 */
#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <ocidl.h>
#include <oleauto.h>
#include <initguid.h>       /* 让 xamlom.h 里的 IID_IXamlDiagnostics 等在本文件里定义 */
#include <xamlom.h>
#include <stdio.h>
#include <wchar.h>

/* {5C1F4E6A-3B7D-4C2E-9A61-2D8E7F0B4A93} */
static const CLSID CLSID_AssistantTap =
    {0x5c1f4e6a, 0x3b7d, 0x4c2e, {0x9a, 0x61, 0x2d, 0x8e, 0x7f, 0x0b, 0x4a, 0x93}};

enum { MODE_PROBE = 0, MODE_CLEAR = 1, MODE_RESTORE = 2 };

/* ------------------------------------------------------------------ 日志 */
static CRITICAL_SECTION g_log_lock;
static void tap_log(const char *fmt, ...)
{
    wchar_t path[MAX_PATH];
    DWORD n = GetTempPathW(MAX_PATH, path);
    if (n == 0 || n > MAX_PATH - 24) return;
    wcscat(path, L"assistant_tap.log");
    EnterCriticalSection(&g_log_lock);
    FILE *f = _wfopen(path, L"a");
    if (f) {
        SYSTEMTIME t;
        GetLocalTime(&t);
        fprintf(f, "%02d:%02d:%02d.%03d [%lu] ", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds,
                GetCurrentThreadId());
        va_list ap;
        va_start(ap, fmt);
        vfprintf(f, fmt, ap);
        va_end(ap);
        fputc('\n', f);
        fclose(f);
    }
    LeaveCriticalSection(&g_log_lock);
}

/* ------------------------------------------------------------------ 小集合（任务栏背景子树里的句柄） */
#define SET_CAP 256
typedef struct { InstanceHandle h[SET_CAP]; int n; } HSet;
static int set_has(const HSet *s, InstanceHandle h)
{
    for (int i = 0; i < s->n; ++i) if (s->h[i] == h) return 1;
    return 0;
}
static void set_add(HSet *s, InstanceHandle h)
{
    if (h && !set_has(s, h) && s->n < SET_CAP) s->h[s->n++] = h;
}
static void set_del(HSet *s, InstanceHandle h)
{
    for (int i = 0; i < s->n; ++i)
        if (s->h[i] == h) { s->h[i] = s->h[--s->n]; return; }
}

/* ------------------------------------------------------------------ 一次连接的上下文 */
typedef struct Tap Tap;
typedef struct {
    IVisualTreeServiceCallback2Vtbl *lpVtbl;
    Tap *owner;
} Callback;

struct Tap {
    IObjectWithSiteVtbl *lpVtbl;        /* 必须是第一个成员：本结构就是 IObjectWithSite */
    LONG refs;
    IUnknown *site;
    IVisualTreeService *vts;
    int mode;
    Callback cb;
    CRITICAL_SECTION lock;
    HSet bg;        /* Taskbar.TaskbarBackground */
    HSet d1, d2;    /* 它下面第 1、2 层 */
    int applied;
};

static LONG g_objects;
static HMODULE g_module;
static Tap *g_watcher;          /* 当前一直订阅着的（clear 模式）连接；新连接来了先把它退订 */
static CRITICAL_SECTION g_state_lock;

static int wcs_eq(BSTR a, const wchar_t *b) { return a && wcscmp(a, b) == 0; }

/* 取属性下标（GetPropertyValuesChain 里按属性名找），用完释放所有 BSTR 和数组 */
static int prop_index(IVisualTreeService *vts, InstanceHandle h, const wchar_t *name, unsigned int *out)
{
    unsigned int sc = 0, pc = 0;
    PropertyChainSource *ps = NULL;
    PropertyChainValue *pv = NULL;
    HRESULT hr = IVisualTreeService_GetPropertyValuesChain(vts, h, &sc, &ps, &pc, &pv);
    int found = 0;
    if (SUCCEEDED(hr)) {
        for (unsigned int i = 0; i < pc; ++i) {
            if (!found && wcs_eq(pv[i].PropertyName, name)) { *out = pv[i].Index; found = 1; }
        }
        for (unsigned int i = 0; i < pc; ++i) {
            SysFreeString(pv[i].Type); SysFreeString(pv[i].DeclaringType); SysFreeString(pv[i].ValueType);
            SysFreeString(pv[i].ItemType); SysFreeString(pv[i].Value); SysFreeString(pv[i].PropertyName);
        }
        for (unsigned int i = 0; i < sc; ++i) {
            SysFreeString(ps[i].TargetType); SysFreeString(ps[i].Name);
            SysFreeString(ps[i].SrcInfo.FileName); SysFreeString(ps[i].SrcInfo.Hash);
        }
        CoTaskMemFree(pv);
        CoTaskMemFree(ps);
    } else {
        tap_log("GetPropertyValuesChain 失败 hr=0x%08lx", (unsigned long)hr);
    }
    return found;
}

static void apply_to(Tap *t, InstanceHandle h, const char *what)
{
    unsigned int idx = 0;
    if (t->mode == MODE_PROBE) {
        tap_log("probe: 找到 %s handle=%llu（不改）", what, (unsigned long long)h);
        return;
    }
    if (!prop_index(t->vts, h, L"Fill", &idx)) {
        tap_log("%s 没找到 Fill 属性", what);
        return;
    }
    HRESULT hr;
    if (t->mode == MODE_CLEAR) {
        BSTR type = SysAllocString(L"Windows.UI.Xaml.Media.SolidColorBrush");
        BSTR val = SysAllocString(L"Transparent");
        InstanceHandle brush = 0;
        hr = IVisualTreeService_CreateInstance(t->vts, type, val, &brush);
        SysFreeString(type);
        SysFreeString(val);
        if (FAILED(hr) || !brush) {
            tap_log("CreateInstance 透明画刷失败 hr=0x%08lx", (unsigned long)hr);
            return;
        }
        hr = IVisualTreeService_SetProperty(t->vts, h, brush, idx);
        tap_log("clear: %s Fill → 透明 hr=0x%08lx", what, (unsigned long)hr);
    } else {
        hr = IVisualTreeService_ClearProperty(t->vts, h, idx);
        tap_log("restore: %s ClearProperty(Fill) hr=0x%08lx", what, (unsigned long)hr);
    }
    if (SUCCEEDED(hr)) InterlockedIncrement((LONG *)&t->applied);
}

/* ------------------------------------------------------------------ IVisualTreeServiceCallback2 */
static Tap *cb_owner(IVisualTreeServiceCallback2 *This) { return ((Callback *)This)->owner; }

static HRESULT STDMETHODCALLTYPE Cb_QueryInterface(IVisualTreeServiceCallback2 *This, REFIID riid, void **ppv)
{
    if (!ppv) return E_POINTER;
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IVisualTreeServiceCallback) ||
        IsEqualIID(riid, &IID_IVisualTreeServiceCallback2)) {
        *ppv = This;
        This->lpVtbl->AddRef(This);
        return S_OK;
    }
    *ppv = NULL;
    return E_NOINTERFACE;
}
/* 回调对象嵌在 Tap 里，引用计数跟着 Tap 走 */
static ULONG STDMETHODCALLTYPE Cb_AddRef(IVisualTreeServiceCallback2 *This)
{
    Tap *t = cb_owner(This);
    return InterlockedIncrement(&t->refs);
}
static ULONG STDMETHODCALLTYPE Tap_Release(IObjectWithSite *This);
static ULONG STDMETHODCALLTYPE Cb_Release(IVisualTreeServiceCallback2 *This)
{
    return Tap_Release((IObjectWithSite *)cb_owner(This));
}

static HRESULT STDMETHODCALLTYPE Cb_OnVisualTreeChange(IVisualTreeServiceCallback2 *This, ParentChildRelation rel,
                                                        VisualElement el, VisualMutationType mut)
{
    Tap *t = cb_owner(This);
    if (!t || !t->vts) return S_OK;
    EnterCriticalSection(&t->lock);
    int target = 0;
    const char *what = NULL;
    if (mut == Add) {
        if (wcs_eq(el.Type, L"Taskbar.TaskbarBackground")) {
            set_add(&t->bg, el.Handle);
        } else if (set_has(&t->bg, rel.Parent)) {
            set_add(&t->d1, el.Handle);
        } else if (set_has(&t->d1, rel.Parent)) {
            set_add(&t->d2, el.Handle);
        }
        int near_bg = set_has(&t->bg, rel.Parent) || set_has(&t->d1, rel.Parent) || set_has(&t->d2, rel.Parent);
        if (near_bg && el.Type && wcsstr(el.Type, L"Rectangle")) {
            if (wcs_eq(el.Name, L"BackgroundFill")) { target = 1; what = "BackgroundFill"; }
            else if (wcs_eq(el.Name, L"BackgroundStroke")) { target = 1; what = "BackgroundStroke"; }
        }
    } else {
        set_del(&t->bg, el.Handle);
        set_del(&t->d1, el.Handle);
        set_del(&t->d2, el.Handle);
    }
    LeaveCriticalSection(&t->lock);
    if (target) apply_to(t, el.Handle, what);
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE Cb_OnElementStateChanged(IVisualTreeServiceCallback2 *This, InstanceHandle h,
                                                           VisualElementState st, LPCWSTR ctx)
{
    (void)This; (void)h; (void)st; (void)ctx;
    return S_OK;
}

static IVisualTreeServiceCallback2Vtbl g_cb_vtbl = {
    Cb_QueryInterface, Cb_AddRef, Cb_Release, Cb_OnVisualTreeChange, Cb_OnElementStateChanged,
};

/* ------------------------------------------------------------------ 订阅线程 */
static void tap_cleanup(Tap *t)
{
    if (t->vts) { IVisualTreeService_Release(t->vts); t->vts = NULL; }
    if (t->site) { IUnknown_Release(t->site); t->site = NULL; }
}

static DWORD WINAPI watch_thread(LPVOID p)
{
    Tap *t = (Tap *)p;
    HRESULT hri = CoInitializeEx(NULL, COINIT_MULTITHREADED);

    /* 上一次一直订阅着的连接（clear 模式）：先退订，免得两个连接同时改 */
    EnterCriticalSection(&g_state_lock);
    Tap *old = g_watcher;
    g_watcher = NULL;
    LeaveCriticalSection(&g_state_lock);
    if (old) {
        /* 只退订、不释放：XAML 线程上可能还有一个正在跑的回调，释放了就是访问已释放内存 → explorer 崩溃。
           每次切换泄漏几十字节，换 explorer 的安全。 */
        if (old->vts) IVisualTreeService_UnadviseVisualTreeChange(old->vts, (IVisualTreeServiceCallback *)&old->cb);
        tap_log("旧连接已退订");
    }

    HRESULT hr = IVisualTreeService_AdviseVisualTreeChange(t->vts, (IVisualTreeServiceCallback *)&t->cb);
    tap_log("AdviseVisualTreeChange hr=0x%08lx mode=%d 任务栏背景 %d 个 处理了 %ld 个", (unsigned long)hr,
            t->mode, t->bg.n, t->applied);
    if (SUCCEEDED(hr) && t->mode == MODE_CLEAR) {
        /* 保持订阅：之后新建的任务栏（接显示器、explorer 重建任务栏）也会被处理 */
        EnterCriticalSection(&g_state_lock);
        g_watcher = t;
        LeaveCriticalSection(&g_state_lock);
    } else if (SUCCEEDED(hr)) {
        /* probe / restore：初次枚举时已经处理完，退订；同样不释放（理由见上） */
        IVisualTreeService_UnadviseVisualTreeChange(t->vts, (IVisualTreeServiceCallback *)&t->cb);
    }
    if (SUCCEEDED(hri)) CoUninitialize();
    return 0;
}

/* ------------------------------------------------------------------ IObjectWithSite */
static HRESULT STDMETHODCALLTYPE Tap_QueryInterface(IObjectWithSite *This, REFIID riid, void **ppv)
{
    if (!ppv) return E_POINTER;
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IObjectWithSite)) {
        *ppv = This;
        This->lpVtbl->AddRef(This);
        return S_OK;
    }
    *ppv = NULL;
    return E_NOINTERFACE;
}
static ULONG STDMETHODCALLTYPE Tap_AddRef(IObjectWithSite *This)
{
    return InterlockedIncrement(&((Tap *)This)->refs);
}
static ULONG STDMETHODCALLTYPE Tap_Release(IObjectWithSite *This)
{
    Tap *t = (Tap *)This;
    LONG r = InterlockedDecrement(&t->refs);
    if (r == 0) {
        tap_cleanup(t);
        DeleteCriticalSection(&t->lock);
        HeapFree(GetProcessHeap(), 0, t);
        InterlockedDecrement(&g_objects);
    }
    return (ULONG)r;
}

static HRESULT STDMETHODCALLTYPE Tap_SetSite(IObjectWithSite *This, IUnknown *site)
{
    Tap *t = (Tap *)This;
    if (!site) return S_OK;                    /* 断开连接：订阅线程会自己收尾 */
    if (t->site) return E_UNEXPECTED;
    IXamlDiagnostics *diag = NULL;
    HRESULT hr = IUnknown_QueryInterface(site, &IID_IXamlDiagnostics, (void **)&diag);
    if (FAILED(hr) || !diag) { tap_log("拿不到 IXamlDiagnostics hr=0x%08lx", (unsigned long)hr); return S_OK; }
    BSTR init = NULL;
    t->mode = MODE_PROBE;
    if (SUCCEEDED(IXamlDiagnostics_GetInitializationData(diag, &init)) && init) {
        if (wcscmp(init, L"clear") == 0) t->mode = MODE_CLEAR;
        else if (wcscmp(init, L"restore") == 0) t->mode = MODE_RESTORE;
        SysFreeString(init);
    }
    IXamlDiagnostics_Release(diag);
    hr = IUnknown_QueryInterface(site, &IID_IVisualTreeService, (void **)&t->vts);
    if (FAILED(hr) || !t->vts) { tap_log("拿不到 IVisualTreeService hr=0x%08lx", (unsigned long)hr); t->vts = NULL; return S_OK; }
    t->site = site;
    IUnknown_AddRef(site);
    tap_log("SetSite：模式 %d（0 探测 / 1 透明 / 2 还原）", t->mode);
    /* 订阅要在别的线程做：在 SetSite 里直接订阅会和 XAML 线程互相等 */
    Tap_AddRef(This);                          /* 线程持有一份引用 */
    HANDLE th = CreateThread(NULL, 0, watch_thread, t, 0, NULL);
    if (!th) { tap_log("CreateThread 失败 %lu", GetLastError()); tap_cleanup(t); Tap_Release(This); return S_OK; }
    CloseHandle(th);
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE Tap_GetSite(IObjectWithSite *This, REFIID riid, void **ppv)
{
    Tap *t = (Tap *)This;
    if (!ppv) return E_POINTER;
    *ppv = NULL;
    if (!t->site) return E_FAIL;
    return IUnknown_QueryInterface(t->site, riid, ppv);
}

static IObjectWithSiteVtbl g_tap_vtbl = {
    Tap_QueryInterface, Tap_AddRef, Tap_Release, Tap_SetSite, Tap_GetSite,
};

/* ------------------------------------------------------------------ 类工厂 */
static HRESULT STDMETHODCALLTYPE Cf_QueryInterface(IClassFactory *This, REFIID riid, void **ppv)
{
    if (!ppv) return E_POINTER;
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IClassFactory)) {
        *ppv = This;
        return S_OK;
    }
    *ppv = NULL;
    return E_NOINTERFACE;
}
static ULONG STDMETHODCALLTYPE Cf_AddRef(IClassFactory *This) { (void)This; return 2; }
static ULONG STDMETHODCALLTYPE Cf_Release(IClassFactory *This) { (void)This; return 1; }
static HRESULT STDMETHODCALLTYPE Cf_CreateInstance(IClassFactory *This, IUnknown *outer, REFIID riid, void **ppv)
{
    (void)This;
    if (!ppv) return E_POINTER;
    *ppv = NULL;
    if (outer) return CLASS_E_NOAGGREGATION;
    Tap *t = (Tap *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(Tap));
    if (!t) return E_OUTOFMEMORY;
    t->lpVtbl = &g_tap_vtbl;
    t->refs = 1;
    t->cb.lpVtbl = &g_cb_vtbl;
    t->cb.owner = t;
    InitializeCriticalSection(&t->lock);
    InterlockedIncrement(&g_objects);
    HRESULT hr = Tap_QueryInterface((IObjectWithSite *)t, riid, ppv);
    Tap_Release((IObjectWithSite *)t);
    return hr;
}
static HRESULT STDMETHODCALLTYPE Cf_LockServer(IClassFactory *This, BOOL lock)
{
    (void)This; (void)lock;
    return S_OK;
}
static IClassFactoryVtbl g_cf_vtbl = {
    Cf_QueryInterface, Cf_AddRef, Cf_Release, Cf_CreateInstance, Cf_LockServer,
};
static IClassFactory g_factory = {&g_cf_vtbl};

/* ------------------------------------------------------------------ 进 explorer 的入口：窗口消息钩子
 * 从外部进程直接调 InitializeXamlDiagnosticsEx 会返回成功但 explorer 不加载（实测）；
 * 所以和 TranslucentTB 一样：助理用 SetWindowsHookEx(WH_CALLWNDPROC) 把本 DLL 挂到任务栏线程，
 * 再给任务栏发一条注册消息（wParam = 模式），我们在 explorer 里对自己调 InitializeXamlDiagnosticsEx。
 * 这之后 DLL 被 XAML 诊断钉在 explorer 里，助理立刻卸掉钩子。 */
typedef HRESULT (WINAPI *PFN_IXDE)(LPCWSTR, DWORD, LPCWSTR, LPCWSTR, CLSID, LPCWSTR);

static void request_in_explorer(int mode)
{
    static const wchar_t *names[] = {L"probe", L"clear", L"restore"};
    if (mode < 0 || mode > 2) return;
    wchar_t self[MAX_PATH];
    if (!GetModuleFileNameW(g_module, self, MAX_PATH)) { tap_log("GetModuleFileName 失败"); return; }
    HMODULE wux = LoadLibraryExW(L"Windows.UI.Xaml.dll", NULL, LOAD_LIBRARY_SEARCH_SYSTEM32);
    PFN_IXDE ixde = wux ? (PFN_IXDE)(void *)GetProcAddress(wux, "InitializeXamlDiagnosticsEx") : NULL;
    if (!ixde) { tap_log("找不到 InitializeXamlDiagnosticsEx"); return; }
    HRESULT hr = HRESULT_FROM_WIN32(ERROR_NOT_FOUND);
    wchar_t ep[64];
    for (int i = 1; i <= 500 && hr == HRESULT_FROM_WIN32(ERROR_NOT_FOUND); ++i) {
        swprintf(ep, 64, L"VisualDiagConnection%d", i);
        hr = ixde(ep, GetCurrentProcessId(), L"", self, CLSID_AssistantTap, names[mode]);
    }
    tap_log("explorer 内部 InitializeXamlDiagnosticsEx(%ls) hr=0x%08lx", names[mode], (unsigned long)hr);
}

LRESULT CALLBACK TapHookProc(int code, WPARAM wp, LPARAM lp)
{
    static UINT req;
    if (!req) req = RegisterWindowMessageW(L"DesktopAssistant.TaskbarTapRequest");
    if (code == HC_ACTION && lp) {
        const CWPSTRUCT *m = (const CWPSTRUCT *)lp;
        if (req && m->message == req) request_in_explorer((int)m->wParam);
    }
    return CallNextHookEx(NULL, code, wp, lp);
}

/* ------------------------------------------------------------------ 导出 */
HRESULT WINAPI DllGetClassObject(REFCLSID clsid, REFIID riid, void **ppv)
{
    if (!ppv) return E_POINTER;
    *ppv = NULL;
    if (!IsEqualCLSID(clsid, &CLSID_AssistantTap)) return CLASS_E_CLASSNOTAVAILABLE;
    return Cf_QueryInterface(&g_factory, riid, ppv);
}

/* 永远不卸载：explorer 里还挂着我们的回调时卸载就是崩溃 */
HRESULT WINAPI DllCanUnloadNow(void) { return S_FALSE; }

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        g_module = inst;
        DisableThreadLibraryCalls(inst);
        InitializeCriticalSection(&g_log_lock);
        InitializeCriticalSection(&g_state_lock);
    }
    return TRUE;
}
