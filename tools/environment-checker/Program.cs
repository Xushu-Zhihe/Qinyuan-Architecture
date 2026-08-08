using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Forms;

[assembly: AssemblyTitle("Qinyuan Environment Checker")]
[assembly: AssemblyDescription("Environment checker for Qinyuan Blackboard")]
[assembly: AssemblyCompany("Qinyuan")]
[assembly: AssemblyProduct("Qinyuan Environment Checker")]
[assembly: AssemblyCopyright("Copyright (c) 2026")]
[assembly: AssemblyVersion("1.2.0.0")]
[assembly: AssemblyFileVersion("1.2.0.0")]
[assembly: AssemblyInformationalVersion("1.2.0")]

namespace Qinyuan.EnvironmentChecker
{
    internal sealed class CheckResult
    {
        public string Name;
        public string Requirement;
        public string Status;
        public string Detail;
        public bool Required;
        public bool Passed;
    }

    internal sealed class CommandResult
    {
        public bool Found;
        public bool TimedOut;
        public int ExitCode;
        public string Executable;
        public string Output;
    }

    internal static class EnvironmentChecks
    {
        private const int CommandTimeoutMilliseconds = 8000;

        public static List<CheckResult> RunAll()
        {
            List<CheckResult> results = new List<CheckResult>();
            results.Add(CheckExactVersion("Python", "3.12.9", "python", "--version", true));
            results.Add(CheckExactVersion("Node.js", "24.18.1", "node", "--version", true));
            results.Add(CheckExactVersion("npm", "11.16.0", "npm", "--version", true));
            results.Add(CheckAvailable("Git", "必须可执行", "git", "--version", true));

            CheckResult codex = CheckAvailable("Codex CLI", "Provider（至少一个）", "codex", "--version", false);
            CheckResult claude = CheckAvailable("Claude CLI", "Provider（至少一个）", "claude", "--version", false);
            CheckResult kimi = CheckAvailable("Kimi CLI", "Provider（至少一个）", "kimi", "--version", false);
            CheckResult qinyuan = CheckAvailable("Qinyuan Main", "可选：Nmap Sandbox Provider", "qinyuan-main", "--version", false);
            results.Add(codex);
            results.Add(claude);
            results.Add(kimi);
            results.Add(qinyuan);
            results.Add(new CheckResult
            {
                Name = "Provider CLI",
                Requirement = "Codex / Claude / Kimi / Qinyuan 至少一个",
                Required = true,
                Passed = codex.Passed || claude.Passed || kimi.Passed || qinyuan.Passed,
                Status = codex.Passed || claude.Passed || kimi.Passed || qinyuan.Passed ? "通过" : "缺失",
                Detail = codex.Passed || claude.Passed || kimi.Passed || qinyuan.Passed
                    ? "已检测到至少一个可用 Provider CLI"
                    : "请安装并认证至少一个 Provider CLI"
            });

            CheckResult dockerCli = CheckAvailable("Docker CLI", "可选：container 后端", "docker", "--version", false);
            results.Add(dockerCli);
            if (dockerCli.Passed)
            {
                CheckResult daemon = CheckAvailable(
                    "Docker daemon",
                    "可选：container 后端",
                    "docker",
                    "version --format {{.Server.Version}}",
                    false
                );
                if (!daemon.Passed)
                {
                    daemon.Status = "可选不可用";
                    daemon.Detail = "Docker CLI 已安装，但守护进程不可用；local 后端不受影响。";
                }
                results.Add(daemon);
                CheckResult toolImage = CheckAvailable(
                    "Nmap 工具镜像",
                    "可选：qinyuan-tools:nmap-1",
                    "docker",
                    "image inspect qinyuan-tools:nmap-1 --format {{.Id}}",
                    false
                );
                results.Add(toolImage);
            }
            else
            {
                results.Add(new CheckResult
                {
                    Name = "Docker daemon",
                    Requirement = "可选：container 后端",
                    Required = false,
                    Passed = false,
                    Status = "可选未安装",
                    Detail = "未检查；local 后端不需要 Docker。"
                });
                results.Add(new CheckResult
                {
                    Name = "Nmap 工具镜像",
                    Requirement = "可选：qinyuan-tools:nmap-1",
                    Required = false,
                    Passed = false,
                    Status = "可选未检查",
                    Detail = "Docker daemon 不可用。"
                });
            }
            results.Add(new CheckResult
            {
                Name = "Sandbox Bridge",
                Requirement = "仅绑定 127.0.0.1",
                Required = true,
                Passed = true,
                Status = "通过",
                Detail = "应用配置固定为 loopback 随机端口；非 loopback 配置会拒绝启动。"
            });
            results.Add(CheckAvailable(
                "Blackboard schema",
                "版本 4 或更高",
                "python",
                "-c \"from orchestrator.store import BLACKBOARD_SCHEMA_VERSION; assert BLACKBOARD_SCHEMA_VERSION >= 4; print(BLACKBOARD_SCHEMA_VERSION)\"",
                false
            ));
            return results;
        }

        private static CheckResult CheckExactVersion(
            string name,
            string expected,
            string command,
            string arguments,
            bool required)
        {
            CommandResult commandResult = RunCommand(command, arguments);
            CheckResult result = FromCommand(name, "固定版本 " + expected, required, commandResult);
            if (commandResult.Found && !commandResult.TimedOut && commandResult.ExitCode == 0)
            {
                string actual = ExtractVersion(commandResult.Output);
                result.Passed = String.Equals(actual, expected, StringComparison.OrdinalIgnoreCase);
                result.Status = result.Passed ? "通过" : "版本不匹配";
                result.Detail = result.Passed
                    ? actual + " · " + commandResult.Executable
                    : "检测到 " + (actual.Length == 0 ? FirstLine(commandResult.Output) : actual)
                        + "，需要 " + expected + " · " + commandResult.Executable;
            }
            return result;
        }

        private static CheckResult CheckAvailable(
            string name,
            string requirement,
            string command,
            string arguments,
            bool required)
        {
            return FromCommand(name, requirement, required, RunCommand(command, arguments));
        }

        private static CheckResult FromCommand(
            string name,
            string requirement,
            bool required,
            CommandResult command)
        {
            CheckResult result = new CheckResult();
            result.Name = name;
            result.Requirement = requirement;
            result.Required = required;
            result.Passed = command.Found && !command.TimedOut && command.ExitCode == 0;
            if (!command.Found)
            {
                result.Status = required ? "缺失" : "可选未安装";
                result.Detail = "PATH 中未找到命令";
            }
            else if (command.TimedOut)
            {
                result.Status = required ? "失败" : "可选不可用";
                result.Detail = "命令在 8 秒内未返回 · " + command.Executable;
            }
            else if (command.ExitCode != 0)
            {
                result.Status = required ? "失败" : "可选不可用";
                result.Detail = "退出码 " + command.ExitCode + " · " + FirstLine(command.Output);
            }
            else
            {
                result.Status = "通过";
                string line = FirstLine(command.Output);
                result.Detail = (line.Length == 0 ? "命令可执行" : line) + " · " + command.Executable;
            }
            return result;
        }

        private static string ExtractVersion(string output)
        {
            Match match = Regex.Match(output ?? String.Empty, @"(?<!\d)(\d+\.\d+\.\d+)(?!\d)");
            return match.Success ? match.Groups[1].Value : String.Empty;
        }

        private static string FirstLine(string value)
        {
            if (String.IsNullOrWhiteSpace(value))
            {
                return String.Empty;
            }
            string normalized = value.Replace("\r", String.Empty).Trim();
            int newline = normalized.IndexOf('\n');
            string line = newline >= 0 ? normalized.Substring(0, newline) : normalized;
            return line.Length > 240 ? line.Substring(0, 240) + "..." : line;
        }

        private static CommandResult RunCommand(string command, string arguments)
        {
            CommandResult result = new CommandResult();
            string executable = FindExecutable(command);
            if (executable == null)
            {
                result.Found = false;
                result.Output = String.Empty;
                result.Executable = command;
                return result;
            }
            result.Found = true;
            result.Executable = executable;
            ProcessStartInfo start = new ProcessStartInfo();
            string extension = Path.GetExtension(executable).ToLowerInvariant();
            if (extension == ".cmd" || extension == ".bat")
            {
                start.FileName = Environment.GetEnvironmentVariable("COMSPEC") ?? "cmd.exe";
                start.Arguments = "/d /s /c \"\"" + executable + "\" " + arguments + "\"";
            }
            else
            {
                start.FileName = executable;
                start.Arguments = arguments;
            }
            start.UseShellExecute = false;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            start.CreateNoWindow = true;
            try
            {
                using (Process process = new Process())
                {
                    process.StartInfo = start;
                    process.Start();
                    string stdout = process.StandardOutput.ReadToEnd();
                    string stderr = process.StandardError.ReadToEnd();
                    if (!process.WaitForExit(CommandTimeoutMilliseconds))
                    {
                        result.TimedOut = true;
                        try { process.Kill(); } catch { }
                    }
                    else
                    {
                        result.ExitCode = process.ExitCode;
                    }
                    result.Output = (stdout + "\n" + stderr).Trim();
                }
            }
            catch (Exception exception)
            {
                result.ExitCode = -1;
                result.Output = exception.Message;
            }
            return result;
        }

        private static string FindExecutable(string command)
        {
            if (command.IndexOf(Path.DirectorySeparatorChar) >= 0 ||
                command.IndexOf(Path.AltDirectorySeparatorChar) >= 0)
            {
                return File.Exists(command) ? Path.GetFullPath(command) : null;
            }
            string path = Environment.GetEnvironmentVariable("PATH") ?? String.Empty;
            string pathExt = Environment.GetEnvironmentVariable("PATHEXT") ?? ".COM;.EXE;.BAT;.CMD";
            List<string> extensions = new List<string>();
            if (Path.HasExtension(command))
            {
                extensions.Add(String.Empty);
            }
            else
            {
                foreach (string extension in pathExt.Split(';'))
                {
                    if (!String.IsNullOrWhiteSpace(extension))
                    {
                        extensions.Add(extension.Trim());
                    }
                }
                extensions.Add(String.Empty);
            }
            foreach (string rawDirectory in path.Split(Path.PathSeparator))
            {
                string directory = rawDirectory.Trim().Trim('"');
                if (directory.Length == 0)
                {
                    continue;
                }
                foreach (string extension in extensions)
                {
                    string candidate;
                    try { candidate = Path.Combine(directory, command + extension); }
                    catch { continue; }
                    if (File.Exists(candidate))
                    {
                        return Path.GetFullPath(candidate);
                    }
                }
            }
            return null;
        }
    }

    internal sealed class MainForm : Form
    {
        private readonly ListView list;
        private readonly Label summary;
        private readonly Button refreshButton;
        private List<CheckResult> currentResults;

        public MainForm()
        {
            Text = "钦原环境检查器 1.2";
            StartPosition = FormStartPosition.CenterScreen;
            MinimumSize = new Size(840, 520);
            Size = new Size(1040, 650);
            Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Regular, GraphicsUnit.Point, 134);
            BackColor = Color.FromArgb(245, 247, 250);

            Label title = new Label();
            title.Text = "钦原运行环境检查";
            title.Font = new Font(Font.FontFamily, 18F, FontStyle.Bold);
            title.ForeColor = Color.FromArgb(24, 39, 67);
            title.AutoSize = true;
            title.Location = new Point(24, 20);

            Label description = new Label();
            description.Text = "检查固定运行时、Git、Provider CLI，以及 container 后端所需的可选 Docker。";
            description.ForeColor = Color.FromArgb(78, 91, 112);
            description.AutoSize = true;
            description.Location = new Point(27, 60);

            list = new ListView();
            list.View = View.Details;
            list.FullRowSelect = true;
            list.GridLines = true;
            list.HideSelection = false;
            list.Location = new Point(24, 92);
            list.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
            list.Size = new Size(ClientSize.Width - 48, ClientSize.Height - 190);
            list.Columns.Add("项目", 175);
            list.Columns.Add("要求", 210);
            list.Columns.Add("状态", 105);
            list.Columns.Add("详情", 470);

            summary = new Label();
            summary.AutoSize = false;
            summary.Location = new Point(24, ClientSize.Height - 86);
            summary.Anchor = AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
            summary.Size = new Size(ClientSize.Width - 420, 48);
            summary.TextAlign = ContentAlignment.MiddleLeft;

            refreshButton = MakeButton("重新检查", ClientSize.Width - 372);
            refreshButton.Click += delegate { StartChecks(); };
            Button copyButton = MakeButton("复制报告", ClientSize.Width - 252);
            copyButton.Click += delegate { CopyReport(); };
            Button closeButton = MakeButton("关闭", ClientSize.Width - 132);
            closeButton.Click += delegate { Close(); };

            Controls.Add(title);
            Controls.Add(description);
            Controls.Add(list);
            Controls.Add(summary);
            Controls.Add(refreshButton);
            Controls.Add(copyButton);
            Controls.Add(closeButton);
            Shown += delegate { StartChecks(); };
        }

        private Button MakeButton(string text, int left)
        {
            Button button = new Button();
            button.Text = text;
            button.Size = new Size(105, 34);
            button.Location = new Point(left, ClientSize.Height - 78);
            button.Anchor = AnchorStyles.Bottom | AnchorStyles.Right;
            return button;
        }

        private void StartChecks()
        {
            refreshButton.Enabled = false;
            list.Items.Clear();
            summary.Text = "正在检查，请稍候...";
            BackgroundWorker worker = new BackgroundWorker();
            worker.DoWork += delegate(object sender, DoWorkEventArgs args)
            {
                args.Result = EnvironmentChecks.RunAll();
            };
            worker.RunWorkerCompleted += delegate(object sender, RunWorkerCompletedEventArgs args)
            {
                refreshButton.Enabled = true;
                if (args.Error != null)
                {
                    summary.Text = "检查失败：" + args.Error.Message;
                    summary.ForeColor = Color.DarkRed;
                    return;
                }
                currentResults = (List<CheckResult>)args.Result;
                RenderResults(currentResults);
            };
            worker.RunWorkerAsync();
        }

        private void RenderResults(List<CheckResult> results)
        {
            list.BeginUpdate();
            list.Items.Clear();
            int requiredFailures = 0;
            foreach (CheckResult result in results)
            {
                ListViewItem item = new ListViewItem(result.Name);
                item.SubItems.Add(result.Requirement);
                item.SubItems.Add(result.Status);
                item.SubItems.Add(result.Detail);
                if (result.Passed)
                {
                    item.ForeColor = Color.FromArgb(22, 112, 67);
                }
                else if (result.Required)
                {
                    item.ForeColor = Color.FromArgb(174, 42, 36);
                    requiredFailures++;
                }
                else
                {
                    item.ForeColor = Color.FromArgb(125, 96, 25);
                }
                list.Items.Add(item);
            }
            list.EndUpdate();
            if (requiredFailures == 0)
            {
                summary.Text = "必需项全部通过。Docker 是可选依赖；仅 container 后端需要。";
                summary.ForeColor = Color.FromArgb(22, 112, 67);
            }
            else
            {
                summary.Text = "有 " + requiredFailures + " 个必需项未通过，请修复后重新检查。";
                summary.ForeColor = Color.FromArgb(174, 42, 36);
            }
        }

        private void CopyReport()
        {
            if (currentResults == null)
            {
                return;
            }
            StringBuilder builder = new StringBuilder();
            builder.AppendLine("钦原环境检查器 1.2");
            foreach (CheckResult result in currentResults)
            {
                builder.AppendLine(result.Name + "\t" + result.Status + "\t" + result.Detail);
            }
            Clipboard.SetText(builder.ToString());
            summary.Text = "报告已复制到剪贴板。";
        }
    }

    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            if (args.Length == 2 && String.Equals(args[0], "--report", StringComparison.OrdinalIgnoreCase))
            {
                List<CheckResult> results = EnvironmentChecks.RunAll();
                WriteJsonReport(args[1], results);
                foreach (CheckResult result in results)
                {
                    if (result.Required && !result.Passed)
                    {
                        return 1;
                    }
                }
                return 0;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new MainForm());
            return 0;
        }

        private static void WriteJsonReport(string path, List<CheckResult> results)
        {
            bool success = true;
            foreach (CheckResult result in results)
            {
                if (result.Required && !result.Passed)
                {
                    success = false;
                }
            }
            StringBuilder builder = new StringBuilder();
            builder.Append("{\n  \"checker_version\": \"1.2.0\",\n");
            builder.Append("  \"generated_at\": \"");
            builder.Append(JsonEscape(DateTimeOffset.UtcNow.ToString("o")));
            builder.Append("\",\n  \"success\": ");
            builder.Append(success ? "true" : "false");
            builder.Append(",\n  \"checks\": [\n");
            for (int index = 0; index < results.Count; index++)
            {
                CheckResult result = results[index];
                builder.Append("    {\"name\": \"");
                builder.Append(JsonEscape(result.Name));
                builder.Append("\", \"requirement\": \"");
                builder.Append(JsonEscape(result.Requirement));
                builder.Append("\", \"status\": \"");
                builder.Append(JsonEscape(result.Status));
                builder.Append("\", \"detail\": \"");
                builder.Append(JsonEscape(result.Detail));
                builder.Append("\", \"required\": ");
                builder.Append(result.Required ? "true" : "false");
                builder.Append(", \"passed\": ");
                builder.Append(result.Passed ? "true" : "false");
                builder.Append("}");
                builder.Append(index + 1 == results.Count ? "\n" : ",\n");
            }
            builder.Append("  ]\n}\n");
            string fullPath = Path.GetFullPath(path);
            string directory = Path.GetDirectoryName(fullPath);
            if (!String.IsNullOrEmpty(directory))
            {
                Directory.CreateDirectory(directory);
            }
            File.WriteAllText(fullPath, builder.ToString(), new UTF8Encoding(false));
        }

        private static string JsonEscape(string value)
        {
            if (value == null)
            {
                return String.Empty;
            }
            StringBuilder builder = new StringBuilder();
            foreach (char character in value)
            {
                switch (character)
                {
                    case '\\': builder.Append("\\\\"); break;
                    case '"': builder.Append("\\\""); break;
                    case '\n': builder.Append("\\n"); break;
                    case '\r': builder.Append("\\r"); break;
                    case '\t': builder.Append("\\t"); break;
                    default:
                        if (character < 32)
                        {
                            builder.Append("\\u" + ((int)character).ToString("x4"));
                        }
                        else
                        {
                            builder.Append(character);
                        }
                        break;
                }
            }
            return builder.ToString();
        }
    }
}
