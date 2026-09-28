// Minimal, fail-closed CEF reference shell for the Zen runtime bakeoff.
// This code is project-authored; CEF is consumed through its public API.

#include <fcntl.h>
#include <limits.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <charconv>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "include/cef_app.h"
#include "include/cef_client.h"
#include "include/cef_command_line.h"
#include "include/cef_download_handler.h"
#include "include/cef_life_span_handler.h"
#include "include/cef_load_handler.h"
#include "include/cef_parser.h"
#include "include/cef_permission_handler.h"
#include "include/cef_request_handler.h"
#include "include/cef_task.h"
#include "include/views/cef_browser_view.h"
#include "include/views/cef_browser_view_delegate.h"
#include "include/views/cef_button_delegate.h"
#include "include/views/cef_fill_layout.h"
#include "include/views/cef_label_button.h"
#include "include/views/cef_window.h"
#include "include/views/cef_window_delegate.h"

namespace {

constexpr char kConfigArgumentPrefix[] = "--zen-config-fd=";
constexpr char kConfigHeader[] = "zenCefShellConfigVersion=1";
constexpr char kSandboxPath[] = "/usr/lib/cef/chrome-sandbox";
constexpr size_t kMaximumConfigBytes = 16U * 1024U;
constexpr int64_t kFixtureActivationDelayMilliseconds = 250;

struct ShellConfig {
  std::string display_backend;
  bool fixture_mode = false;
  std::string profile_root;
  std::string start_url;
  bool test_auto_activate = false;
};

bool HasControlCharacter(std::string_view value) {
  for (const char character : value) {
    const auto byte = static_cast<unsigned char>(character);
    if (byte < 0x20U || byte == 0x7fU) {
      return true;
    }
  }
  return false;
}

bool IsLexicallyNormalizedAbsolutePath(std::string_view value) {
  if (value.empty() || value.front() != '/' || value.size() > PATH_MAX ||
      HasControlCharacter(value)) {
    return false;
  }
  if (value.size() > 1U && value.back() == '/') {
    return false;
  }
  size_t offset = 1U;
  while (offset <= value.size()) {
    const size_t separator = value.find('/', offset);
    const size_t end = separator == std::string_view::npos ? value.size()
                                                           : separator;
    const std::string_view component = value.substr(offset, end - offset);
    if (component.empty() || component == "." || component == "..") {
      return false;
    }
    if (separator == std::string_view::npos) {
      break;
    }
    offset = separator + 1U;
  }
  return value != "/";
}

std::optional<int> ParseDescriptor(std::string_view argument) {
  if (!argument.starts_with(kConfigArgumentPrefix)) {
    return std::nullopt;
  }
  const std::string_view digits = argument.substr(
      std::char_traits<char>::length(kConfigArgumentPrefix));
  if (digits.empty()) {
    return std::nullopt;
  }
  int descriptor = -1;
  const auto conversion =
      std::from_chars(digits.data(), digits.data() + digits.size(), descriptor);
  if (conversion.ec != std::errc() || conversion.ptr != digits.data() + digits.size() ||
      descriptor < 3) {
    return std::nullopt;
  }
  return descriptor;
}

bool HasRequiredSeals(int descriptor) {
  const int seals = fcntl(descriptor, F_GET_SEALS);
  if (seals < 0) {
    return false;
  }
  constexpr int required =
      F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL;
  return (seals & required) == required;
}

std::optional<std::string> ReadSealedConfig(int descriptor) {
  struct stat status {};
  if (fstat(descriptor, &status) != 0 || !S_ISREG(status.st_mode) ||
      status.st_size <= 0 ||
      static_cast<uintmax_t>(status.st_size) > kMaximumConfigBytes ||
      !HasRequiredSeals(descriptor)) {
    return std::nullopt;
  }
  std::string payload(static_cast<size_t>(status.st_size), '\0');
  size_t offset = 0U;
  while (offset < payload.size()) {
    const ssize_t count = pread(descriptor, payload.data() + offset,
                                payload.size() - offset,
                                static_cast<off_t>(offset));
    if (count <= 0) {
      return std::nullopt;
    }
    offset += static_cast<size_t>(count);
  }
  struct stat final_status {};
  if (fstat(descriptor, &final_status) != 0 ||
      final_status.st_dev != status.st_dev || final_status.st_ino != status.st_ino ||
      final_status.st_size != status.st_size || !HasRequiredSeals(descriptor)) {
    return std::nullopt;
  }
  return payload;
}

std::optional<std::string> RemovePrefix(std::string_view value,
                                        std::string_view prefix) {
  if (!value.starts_with(prefix)) {
    return std::nullopt;
  }
  return std::string(value.substr(prefix.size()));
}

std::optional<ShellConfig> ParseConfig(std::string_view payload) {
  if (payload.empty() || payload.back() != '\n') {
    return std::nullopt;
  }
  std::vector<std::string_view> lines;
  size_t offset = 0U;
  while (offset < payload.size()) {
    const size_t newline = payload.find('\n', offset);
    if (newline == std::string_view::npos) {
      return std::nullopt;
    }
    lines.push_back(payload.substr(offset, newline - offset));
    offset = newline + 1U;
  }
  for (const std::string_view line : lines) {
    if (HasControlCharacter(line)) {
      return std::nullopt;
    }
  }
  if (lines.size() != 6U || lines[0] != kConfigHeader) {
    return std::nullopt;
  }
  const auto display = RemovePrefix(lines[1], "displayBackend=");
  const auto fixture = RemovePrefix(lines[2], "fixtureMode=");
  const auto profile = RemovePrefix(lines[3], "profileRoot=");
  const auto start_url = RemovePrefix(lines[4], "startUrl=");
  const auto auto_activate = RemovePrefix(lines[5], "testAutoActivate=");
  if (!display || !fixture || !profile || !start_url || !auto_activate ||
      (*display != "wayland" && *display != "x11") ||
      (*fixture != "0" && *fixture != "1") ||
      (*auto_activate != "0" && *auto_activate != "1") ||
      !IsLexicallyNormalizedAbsolutePath(*profile) || start_url->empty() ||
      start_url->size() > 4096U || HasControlCharacter(*start_url)) {
    return std::nullopt;
  }
  ShellConfig config;
  config.display_backend = *display;
  config.fixture_mode = *fixture == "1";
  config.profile_root = *profile;
  config.start_url = *start_url;
  config.test_auto_activate = *auto_activate == "1";
  if (config.test_auto_activate && !config.fixture_mode) {
    return std::nullopt;
  }
  if (config.fixture_mode) {
    if (!std::string_view(config.start_url).starts_with("http://127.0.0.1:")) {
      return std::nullopt;
    }
  } else if (!std::string_view(config.start_url).starts_with("https://")) {
    return std::nullopt;
  }
  return config;
}

bool ValidatePrivateProfileRoot(const std::string& path) {
  struct stat status {};
  if (lstat(path.c_str(), &status) != 0 || !S_ISDIR(status.st_mode) ||
      status.st_uid != geteuid() || (status.st_mode & 07777U) != 0700U) {
    return false;
  }
  char resolved[PATH_MAX + 1] = {};
  return realpath(path.c_str(), resolved) != nullptr && path == resolved;
}

bool ConfigureSandbox() {
  struct stat status {};
  if (lstat(kSandboxPath, &status) != 0 || !S_ISREG(status.st_mode) ||
      status.st_uid != 0U || status.st_gid != 0U || status.st_nlink != 1U ||
      (status.st_mode & 07777U) != 04755U) {
    return false;
  }
  return setenv("CHROME_DEVEL_SANDBOX", kSandboxPath, 1) == 0;
}

std::optional<std::string> ExecutablePath() {
  std::vector<char> buffer(static_cast<size_t>(PATH_MAX) + 1U, '\0');
  const ssize_t length =
      readlink("/proc/self/exe", buffer.data(), buffer.size() - 1U);
  if (length <= 0 || static_cast<size_t>(length) >= buffer.size() - 1U) {
    return std::nullopt;
  }
  return std::string(buffer.data(), static_cast<size_t>(length));
}

std::optional<std::string> ParentDirectory(const std::string& path) {
  const size_t separator = path.rfind('/');
  if (separator == std::string::npos || separator == 0U) {
    return std::nullopt;
  }
  return path.substr(0U, separator);
}

std::optional<std::string> OriginForURL(const CefString& value) {
  CefURLParts parts;
  if (!CefParseURL(value, parts)) {
    return std::nullopt;
  }
  const std::string scheme = CefString(&parts.scheme).ToString();
  const std::string username = CefString(&parts.username).ToString();
  const std::string password = CefString(&parts.password).ToString();
  const std::string origin = CefString(&parts.origin).ToString();
  if ((scheme != "https" && scheme != "http") || !username.empty() ||
      !password.empty() || origin.empty()) {
    return std::nullopt;
  }
  return origin;
}

class ShellClient final : public CefClient,
                          public CefLifeSpanHandler,
                          public CefLoadHandler,
                          public CefPermissionHandler,
                          public CefDownloadHandler,
                          public CefRequestHandler {
 public:
  explicit ShellClient(std::string allowed_origin)
      : allowed_origin_(std::move(allowed_origin)) {}

  CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override { return this; }
  CefRefPtr<CefLoadHandler> GetLoadHandler() override { return this; }
  CefRefPtr<CefPermissionHandler> GetPermissionHandler() override {
    return this;
  }
  CefRefPtr<CefDownloadHandler> GetDownloadHandler() override { return this; }
  CefRefPtr<CefRequestHandler> GetRequestHandler() override { return this; }

  bool OnBeforePopup(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame>, int,
                     const CefString&, const CefString&, WindowOpenDisposition,
                     bool, const CefPopupFeatures&, CefWindowInfo&,
                     CefRefPtr<CefClient>&, CefBrowserSettings&,
                     CefRefPtr<CefDictionaryValue>&, bool*) override {
    std::cerr << "ZEN_CEF_POPUP_DENIED\n" << std::flush;
    return true;
  }

  void OnAfterCreated(CefRefPtr<CefBrowser> browser) override {
    browser_ = browser;
    std::cerr << "ZEN_CEF_BROWSER_CREATED\n" << std::flush;
  }

  void OnBeforeClose(CefRefPtr<CefBrowser>) override {
    browser_ = nullptr;
    CefQuitMessageLoop();
  }

  bool OnBeforeBrowse(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame,
                      CefRefPtr<CefRequest> request, bool, bool) override {
    if (!frame->IsMain()) {
      return false;
    }
    const auto target_origin = OriginForURL(request->GetURL());
    const bool deny = !target_origin || *target_origin != allowed_origin_;
    if (deny) {
      std::cerr << "ZEN_CEF_CROSS_ORIGIN_NAVIGATION_DENIED\n" << std::flush;
    }
    return deny;
  }

  bool OnOpenURLFromTab(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame>,
                        const CefString&, WindowOpenDisposition,
                        bool) override {
    std::cerr << "ZEN_CEF_NEW_SURFACE_DENIED\n" << std::flush;
    return true;
  }

  bool OnCertificateError(CefRefPtr<CefBrowser>, cef_errorcode_t,
                          const CefString&, CefRefPtr<CefSSLInfo>,
                          CefRefPtr<CefCallback>) override {
    return false;
  }

  bool OnRequestMediaAccessPermission(
      CefRefPtr<CefBrowser>, CefRefPtr<CefFrame>, const CefString&,
      uint32_t, CefRefPtr<CefMediaAccessCallback> callback) override {
    callback->Cancel();
    std::cerr << "ZEN_CEF_MEDIA_PERMISSION_DENIED\n" << std::flush;
    return true;
  }

  bool OnShowPermissionPrompt(
      CefRefPtr<CefBrowser>, uint64_t, const CefString&, uint32_t,
      CefRefPtr<CefPermissionPromptCallback> callback) override {
    callback->Continue(CEF_PERMISSION_RESULT_DENY);
    std::cerr << "ZEN_CEF_PERMISSION_DENIED\n" << std::flush;
    return true;
  }

  bool CanDownload(CefRefPtr<CefBrowser>, const CefString& url,
                   const CefString&) override {
    const auto target_origin = OriginForURL(url);
    return target_origin && *target_origin == allowed_origin_;
  }

  bool OnBeforeDownload(CefRefPtr<CefBrowser>, CefRefPtr<CefDownloadItem>,
                        const CefString&,
                        CefRefPtr<CefBeforeDownloadCallback> callback) override {
    callback->Continue(CefString(), true);
    std::cerr << "ZEN_CEF_DOWNLOAD_SYSTEM_PICKER_REQUESTED\n" << std::flush;
    return true;
  }

  void OnLoadEnd(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame,
                 int http_status_code) override {
    if (frame->IsMain()) {
      std::cerr << "ZEN_CEF_CONTENT_COMMITTED " << http_status_code << "\n"
                << std::flush;
    }
  }

  CefRefPtr<CefBrowser> browser() const { return browser_; }

 private:
  const std::string allowed_origin_;
  CefRefPtr<CefBrowser> browser_;

  IMPLEMENT_REFCOUNTING(ShellClient);
  DISALLOW_COPY_AND_ASSIGN(ShellClient);
};

class ShellWindowDelegate;

class AutoActivateTask final : public CefTask {
 public:
  explicit AutoActivateTask(CefRefPtr<ShellWindowDelegate> delegate)
      : delegate_(std::move(delegate)) {}

  void Execute() override;

 private:
  CefRefPtr<ShellWindowDelegate> delegate_;

  IMPLEMENT_REFCOUNTING(AutoActivateTask);
  DISALLOW_COPY_AND_ASSIGN(AutoActivateTask);
};

class ShellWindowDelegate final : public CefWindowDelegate,
                                  public CefButtonDelegate,
                                  public CefBrowserViewDelegate {
 public:
  ShellWindowDelegate(ShellConfig config, CefRefPtr<ShellClient> client)
      : config_(std::move(config)), client_(std::move(client)) {}

  void OnWindowCreated(CefRefPtr<CefWindow> window) override {
    window_ = window;
    window_->SetTitle("Zen CEF Candidate");
    window_->SetToFillLayout();
    button_ = CefLabelButton::CreateLabelButton(this, "Open App");
    button_->SetMinimumSize(CefSize(320, 180));
    window_->AddChildView(button_);
    window_->CenterWindow(CefSize(960, 640));
    window_->Show();
    std::cerr << "ZEN_CEF_ONBOARDING_READY\n" << std::flush;
    if (config_.test_auto_activate) {
      CefPostDelayedTask(TID_UI, new AutoActivateTask(this),
                         kFixtureActivationDelayMilliseconds);
    }
  }

  void OnWindowDestroyed(CefRefPtr<CefWindow>) override {
    browser_view_ = nullptr;
    button_ = nullptr;
    window_ = nullptr;
    if (!client_->browser()) {
      CefQuitMessageLoop();
    }
  }

  CefSize GetPreferredSize(CefRefPtr<CefView>) override {
    return CefSize(960, 640);
  }

  cef_runtime_style_t GetWindowRuntimeStyle() override {
    return CEF_RUNTIME_STYLE_ALLOY;
  }

  bool CanClose(CefRefPtr<CefWindow>) override {
    CefRefPtr<CefBrowser> browser = client_->browser();
    if (browser) {
      return browser->GetHost()->TryCloseBrowser();
    }
    return true;
  }

  void OnButtonPressed(CefRefPtr<CefButton>) override {
    CefPostTask(TID_UI, new AutoActivateTask(this));
  }

  void Activate() {
    if (activated_ || !window_) {
      return;
    }
    activated_ = true;
    std::cerr << "ZEN_CEF_CONTENT_REQUESTED\n" << std::flush;
    window_->RemoveAllChildViews();
    button_ = nullptr;
    CefBrowserSettings browser_settings;
    browser_settings.javascript = STATE_ENABLED;
    browser_settings.local_storage = STATE_ENABLED;
    browser_settings.webgl = STATE_ENABLED;
    browser_settings.javascript_access_clipboard = STATE_ENABLED;
    browser_view_ = CefBrowserView::CreateBrowserView(
        client_, config_.start_url, browser_settings, nullptr, nullptr, this);
    window_->AddChildView(browser_view_);
    window_->Layout();
  }

  bool OnPopupBrowserViewCreated(CefRefPtr<CefBrowserView>,
                                 CefRefPtr<CefBrowserView> popup_browser_view,
                                 bool) override {
    if (popup_browser_view && popup_browser_view->GetBrowser()) {
      popup_browser_view->GetBrowser()->GetHost()->CloseBrowser(true);
    }
    return true;
  }

  cef_runtime_style_t GetBrowserRuntimeStyle() override {
    return CEF_RUNTIME_STYLE_ALLOY;
  }

 private:
  const ShellConfig config_;
  const CefRefPtr<ShellClient> client_;
  bool activated_ = false;
  CefRefPtr<CefWindow> window_;
  CefRefPtr<CefLabelButton> button_;
  CefRefPtr<CefBrowserView> browser_view_;

  IMPLEMENT_REFCOUNTING(ShellWindowDelegate);
  DISALLOW_COPY_AND_ASSIGN(ShellWindowDelegate);
};

void AutoActivateTask::Execute() {
  delegate_->Activate();
  delegate_ = nullptr;
}

class ShellApp final : public CefApp, public CefBrowserProcessHandler {
 public:
  explicit ShellApp(ShellConfig config) : config_(std::move(config)) {}

  CefRefPtr<CefBrowserProcessHandler> GetBrowserProcessHandler() override {
    return this;
  }

  void OnBeforeCommandLineProcessing(
      const CefString& process_type,
      CefRefPtr<CefCommandLine> command_line) override {
    if (!process_type.empty()) {
      command_line->RemoveSwitch("user-data-dir");
      command_line->RemoveSwitch("zen-config-fd");
      return;
    }
    command_line->AppendSwitchWithValue("ozone-platform",
                                        config_.display_backend);
    command_line->AppendSwitch("disable-background-networking");
    command_line->AppendSwitch("disable-breakpad");
    command_line->AppendSwitch("disable-component-update");
    command_line->AppendSwitch("disable-domain-reliability");
    command_line->AppendSwitch("disable-extensions");
    command_line->AppendSwitch("disable-sync");
    command_line->AppendSwitch("disable-translate");
    command_line->AppendSwitch("no-default-browser-check");
    command_line->AppendSwitch("no-first-run");
    command_line->AppendSwitchWithValue("renderer-process-limit", "2");
  }

  void OnBeforeChildProcessLaunch(
      CefRefPtr<CefCommandLine> command_line) override {
    command_line->RemoveSwitch("user-data-dir");
    command_line->RemoveSwitch("zen-config-fd");
  }

  void OnContextInitialized() override {
    const auto origin = OriginForURL(config_.start_url);
    if (!origin || (!config_.fixture_mode && !origin->starts_with("https://")) ||
        (config_.fixture_mode &&
         !origin->starts_with("http://127.0.0.1:"))) {
      std::cerr << "ZEN_CEF_FATAL invalid-start-origin\n" << std::flush;
      CefQuitMessageLoop();
      return;
    }
    client_ = new ShellClient(*origin);
    window_delegate_ = new ShellWindowDelegate(config_, client_);
    CefWindow::CreateTopLevelWindow(window_delegate_);
  }

  bool OnAlreadyRunningAppRelaunch(CefRefPtr<CefCommandLine>,
                                   const CefString&) override {
    return true;
  }

 private:
  const ShellConfig config_;
  CefRefPtr<ShellClient> client_;
  CefRefPtr<ShellWindowDelegate> window_delegate_;

  IMPLEMENT_REFCOUNTING(ShellApp);
  DISALLOW_COPY_AND_ASSIGN(ShellApp);
};

class ShellSubprocessApp final : public CefApp {
 public:
  ShellSubprocessApp() = default;

  void OnBeforeCommandLineProcessing(
      const CefString& process_type,
      CefRefPtr<CefCommandLine> command_line) override {
    if (!process_type.empty()) {
      command_line->RemoveSwitch("user-data-dir");
      command_line->RemoveSwitch("zen-config-fd");
    }
  }

 private:
  IMPLEMENT_REFCOUNTING(ShellSubprocessApp);
  DISALLOW_COPY_AND_ASSIGN(ShellSubprocessApp);
};

int Fail(const char* code) {
  std::cerr << "ZEN_CEF_FATAL " << code << "\n" << std::flush;
  return 2;
}

}  // namespace

NO_STACK_PROTECTOR
int main(int argc, char* argv[]) {
  CefMainArgs main_args(argc, argv);
  CefRefPtr<CefApp> subprocess_app(new ShellSubprocessApp());
  const int child_exit = CefExecuteProcess(main_args, subprocess_app, nullptr);
  if (child_exit >= 0) {
    return child_exit;
  }

  if (argc != 2) {
    return Fail("closed-arguments");
  }
  const auto config_descriptor = ParseDescriptor(argv[1]);
  if (!config_descriptor) {
    return Fail("invalid-config-descriptor");
  }
  const auto config_payload = ReadSealedConfig(*config_descriptor);
  if (!config_payload) {
    return Fail("unsealed-config");
  }
  const auto config = ParseConfig(*config_payload);
  if (!config) {
    return Fail("invalid-config");
  }
  if (!ValidatePrivateProfileRoot(config->profile_root)) {
    return Fail("unsafe-profile-root");
  }
  if (!ConfigureSandbox()) {
    return Fail("sandbox-authority");
  }
  const auto executable_path = ExecutablePath();
  if (!executable_path) {
    return Fail("executable-path");
  }
  const auto runtime_directory = ParentDirectory(*executable_path);
  if (!runtime_directory) {
    return Fail("runtime-directory");
  }

  CefSettings settings;
  settings.no_sandbox = false;
  settings.command_line_args_disabled = true;
  settings.multi_threaded_message_loop = false;
  settings.windowless_rendering_enabled = false;
  settings.persist_session_cookies = true;
  settings.remote_debugging_port = 0;
  settings.log_severity = LOGSEVERITY_DISABLE;
  settings.background_color = CefColorSetARGB(0xff, 0x16, 0x18, 0x1d);
  CefString(&settings.browser_subprocess_path) = *executable_path;
  CefString(&settings.root_cache_path) = config->profile_root;
  CefString(&settings.cache_path) = config->profile_root + "/default";
  CefString(&settings.resources_dir_path) = *runtime_directory;
  CefString(&settings.locales_dir_path) = *runtime_directory + "/locales";
  CefString(&settings.locale) = "en-US";
  CefString(&settings.accept_language_list) = "en-US,en";

  CefRefPtr<ShellApp> app(new ShellApp(*config));
  if (!CefInitialize(main_args, settings, app, nullptr)) {
    return CefGetExitCode();
  }
  CefRunMessageLoop();
  CefShutdown();
  return 0;
}
