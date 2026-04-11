using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;

namespace TicketBotWarRoom;

public partial class MainWindow : Window
{
    private readonly ObservableCollection<BotInstance> _instances = new();
    private readonly string _projectDir;

    public MainWindow()
    {
        InitializeComponent();

        _projectDir = Path.GetFullPath(
            Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", "..", "..", ".."));

        BotGridDisplay.ItemsSource = _instances;
    }

    // ── 初始化格子 ──
    private void InitializeGrid_Click(object sender, RoutedEventArgs e)
    {
        StopAllProcesses();
        _instances.Clear();

        if (!int.TryParse(InstanceCountInput.Text, out int n) || n < 1)
        {
            MessageBox.Show("請輸入正整數", "War-Room", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }

        int cols = n switch { 1 => 1, <= 4 => 2, <= 9 => 3, _ => 4 };
        BotGridDisplay.ItemsPanel = CreateUniformGridTemplate(cols);

        for (int i = 0; i < n; i++)
        {
            _instances.Add(new BotInstance
            {
                Id = i,
                DisplayName = $"ACC-{i}",
                Status = "待命",
                CfgPlatform = "TIXCRAFT",
                CfgActivitySlug = "26_mltr",
                CfgTicketAmount = "1",
                CfgAreaKeyword = "",
                CfgAreaMode = "關鍵字優先",
                CfgExcludeKeyword = "輪椅;身障;身心;障礙;Restricted View;燈柱遮蔽;視線不完整;身障票",
                CfgDateKeyword = "",
                CfgPresaleCode = "",
                CfgTargetTime = "12:00:00",
                CfgEnableTimer = true,
                CfgWatchUrl = "",
                CfgCookie = "",
                CfgRunMode = "API模式",
            });
        }
    }

    private static ItemsPanelTemplate CreateUniformGridTemplate(int columns)
    {
        var xaml = $@"
            <ItemsPanelTemplate xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation'>
                <UniformGrid Columns='{columns}' Margin='8'/>
            </ItemsPanelTemplate>";
        return (ItemsPanelTemplate)System.Windows.Markup.XamlReader.Parse(xaml);
    }

    // ── 全部啟動 / 停止 ──
    private void StartAll_Click(object sender, RoutedEventArgs e)
    {
        if (_instances.Count == 0) { MessageBox.Show("請先初始化"); return; }
        foreach (var inst in _instances) LaunchIfNotRunning(inst);
    }

    private void StopAll_Click(object sender, RoutedEventArgs e) => StopAllProcesses();

    // ── 單一實例操作 ──
    private void StartOne_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement fe && fe.Tag is BotInstance inst)
            LaunchIfNotRunning(inst);
    }

    private void StopOne_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement fe && fe.Tag is BotInstance inst)
            StopOne(inst);
    }

    private void PauseOne_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { Tag: BotInstance inst } fe)
        {
            var pauseFile = Path.Combine(_projectDir, "profiles", $"acc_{inst.Id}", "pause.lock");
            Directory.CreateDirectory(Path.GetDirectoryName(pauseFile)!);

            if (File.Exists(pauseFile))
            {
                File.Delete(pauseFile);
                inst.AppendLog("[PAUSE] 已繼續");
                if (fe is Button btn) btn.Content = "暫停";
            }
            else
            {
                File.WriteAllText(pauseFile, "paused");
                inst.AppendLog("[PAUSE] 已暫停");
                if (fe is Button btn) btn.Content = "繼續";
            }
        }
    }

    private void Close_Click(object sender, RoutedEventArgs e) => Close();

    // ── 進程管理 ──
    private void LaunchIfNotRunning(BotInstance inst)
    {
        if (inst.Proc is not null && !inst.Proc.HasExited) return;
        LaunchBot(inst);
    }

    private void LaunchBot(BotInstance inst)
    {
        // 清除暫停檔
        var profileDir = Path.Combine(_projectDir, "profiles", $"acc_{inst.Id}");
        Directory.CreateDirectory(profileDir);
        var pauseFile = Path.Combine(profileDir, "pause.lock");
        if (File.Exists(pauseFile)) File.Delete(pauseFile);

        // 寫出 per-instance config JSON
        var configPath = Path.Combine(profileDir, "config.json");
        var cfg = new Dictionary<string, object>
        {
            ["PLATFORM"] = inst.CfgPlatform,
            ["COOKIE"] = inst.CfgCookie,
            ["ACTIVITY_SLUG"] = inst.CfgActivitySlug,
            ["TICKET_AMOUNT"] = inst.CfgTicketAmount,
            ["AREA_KEYWORD"] = inst.CfgAreaKeyword,
            ["AREA_AUTO_SELECT_MODE"] = inst.CfgAreaMode,
            ["EXCLUDE_AREA_KEYWORD"] = inst.CfgExcludeKeyword,
            ["DATE_KEYWORD"] = inst.CfgDateKeyword,
            ["PRESALE_CODE"] = inst.CfgPresaleCode,
            ["TARGET_START_TIME"] = inst.CfgTargetTime,
            ["ENABLE_TIME_WATCHER"] = inst.CfgEnableTimer,
            ["TIME_WATCH_URL"] = inst.CfgWatchUrl,
        };
        File.WriteAllText(configPath,
            JsonSerializer.Serialize(cfg, new JsonSerializerOptions { WriteIndented = true }));

        // API模式 → test.py | 瀏覽器模式 → main.py
        bool isApi = inst.CfgRunMode == "API模式";
        var scriptPath = Path.Combine(_projectDir, isApi ? "test.py" : "main.py");
        var args = isApi
            ? $"\"{scriptPath}\" --config \"{configPath}\""
            : $"\"{scriptPath}\" --acc {inst.Id} --config \"{configPath}\"";

        var psi = new ProcessStartInfo
        {
            FileName = "python",
            Arguments = args,
            WorkingDirectory = _projectDir,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = System.Text.Encoding.UTF8,
            StandardErrorEncoding = System.Text.Encoding.UTF8,
        };

        var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };

        proc.OutputDataReceived += (_, args) =>
        {
            if (args.Data is null) return;
            Dispatcher.BeginInvoke(() => inst.AppendLog(args.Data));
        };
        proc.ErrorDataReceived += (_, args) =>
        {
            if (args.Data is null) return;
            Dispatcher.BeginInvoke(() => inst.AppendLog($"[ERR] {args.Data}"));
        };
        proc.Exited += (_, _) =>
        {
            Dispatcher.BeginInvoke(() => inst.Status = $"已結束 (code {proc.ExitCode})");
        };

        try
        {
            proc.Start();
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();
            inst.Proc = proc;
            inst.Status = $"運行中 (PID {proc.Id})";
            inst.Logs = "";
        }
        catch (Exception ex)
        {
            inst.Status = "啟動失敗";
            inst.AppendLog($"[ERROR] {ex.Message}");
        }
    }

    private static void StopOne(BotInstance inst)
    {
        if (inst.Proc is null || inst.Proc.HasExited) return;
        try { inst.Proc.Kill(entireProcessTree: true); } catch { }
        inst.Status = "已停止";
    }

    private void StopAllProcesses()
    {
        foreach (var inst in _instances) StopOne(inst);
    }

    private void Log_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (sender is TextBox tb) tb.ScrollToEnd();
    }

    protected override void OnClosing(CancelEventArgs e)
    {
        StopAllProcesses();
        base.OnClosing(e);
    }
}

// ── ViewModel ──
public class BotInstance : INotifyPropertyChanged
{
    public int Id { get; set; }
    public Process? Proc { get; set; }

    private string _displayName = "";
    public string DisplayName { get => _displayName; set { _displayName = value; N(); } }

    private string _status = "待命";
    public string Status { get => _status; set { _status = value; N(); } }

    private string _logs = "";
    public string Logs { get => _logs; set { _logs = value; N(); } }

    // ── Config fields ──
    private string _cfgPlatform = "TIXCRAFT";
    public string CfgPlatform { get => _cfgPlatform; set { _cfgPlatform = value; N(); } }

    private string _cfgCookie = "";
    public string CfgCookie { get => _cfgCookie; set { _cfgCookie = value; N(); } }

    private string _cfgActivitySlug = "";
    public string CfgActivitySlug { get => _cfgActivitySlug; set { _cfgActivitySlug = value; N(); } }

    private string _cfgTicketAmount = "1";
    public string CfgTicketAmount { get => _cfgTicketAmount; set { _cfgTicketAmount = value; N(); } }

    private string _cfgAreaKeyword = "";
    public string CfgAreaKeyword { get => _cfgAreaKeyword; set { _cfgAreaKeyword = value; N(); } }

    private string _cfgAreaMode = "關鍵字優先";
    public string CfgAreaMode { get => _cfgAreaMode; set { _cfgAreaMode = value; N(); } }

    private string _cfgExcludeKeyword = "";
    public string CfgExcludeKeyword { get => _cfgExcludeKeyword; set { _cfgExcludeKeyword = value; N(); } }

    private string _cfgDateKeyword = "";
    public string CfgDateKeyword { get => _cfgDateKeyword; set { _cfgDateKeyword = value; N(); } }

    private string _cfgPresaleCode = "";
    public string CfgPresaleCode { get => _cfgPresaleCode; set { _cfgPresaleCode = value; N(); } }

    private string _cfgTargetTime = "12:00:00";
    public string CfgTargetTime { get => _cfgTargetTime; set { _cfgTargetTime = value; N(); } }

    private bool _cfgEnableTimer = true;
    public bool CfgEnableTimer { get => _cfgEnableTimer; set { _cfgEnableTimer = value; N(); } }

    private string _cfgWatchUrl = "";
    public string CfgWatchUrl { get => _cfgWatchUrl; set { _cfgWatchUrl = value; N(); } }

    private string _cfgRunMode = "API模式";
    public string CfgRunMode { get => _cfgRunMode; set { _cfgRunMode = value; N(); } }

    // ── Log ──
    private const int MaxLogLen = 50_000;
    public void AppendLog(string line)
    {
        if (_logs.Length > MaxLogLen)
            _logs = _logs[(_logs.Length - MaxLogLen / 2)..];
        _logs += line + "\n";
        N(nameof(Logs));
    }

    public event PropertyChangedEventHandler? PropertyChanged;
    private void N([CallerMemberName] string? name = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}
