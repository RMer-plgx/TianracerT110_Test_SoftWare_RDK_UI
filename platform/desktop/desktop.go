// Package desktop contains the native MyGo UI spike. The sample state is
// local and disposable; the window has no runtime or device connection.
package desktop

import (
	"fmt"
	"strings"
	"time"

	"github.com/egoist/mygo"
	"github.com/egoist/mygo/ui"
)

type sample struct {
	page, theme     string
	device, address string
	operator, query string
	formError       string
	selected        int
	session         string
}

type check struct {
	title, category, state, verdict, note, measured, criterion string
}

var checks = []check{
	{"设备身份", "连接与预检", "已完成", "PASS", "虚拟设备标识与本轮样例匹配。", "SIM-01", "匹配演示设备标识"},
	{"电压采样", "连接与预检", "已完成", "WARN", "样例读数用于展示测量值和判定；数值并非 T110 标准。", "24.1 V", "合成阈值 24.5–28.0 V"},
	{"遥测回读", "只读采集", "采样中", "PENDING", "当前仅演示等待数据时的界面。", "尚无样本", "需要有效样本"},
	{"接口检查", "只读采集", "排队中", "PENDING", "未执行。", "—", "—"},
	{"证据归档", "记录", "排队中", "PENDING", "未执行。", "—", "—"},
}

func Run(theme, page string, width, height int, autoClose time.Duration) error {
	state := &sample{page: page, theme: theme, device: "SIM-01", address: "sim://bench-01", selected: 1, session: "idle"}
	if page == "checks" {
		state.session = "running"
	}
	mygo.App.SetName("TB Verify · P0")
	mygo.App.WhenReady(func() {
		mygo.Theme.SetSource(mygo.ThemeSource(theme))
		win := mygo.NewWindow(mygo.WindowOptions{
			Title: "TB Verify · P0", Width: width, Height: height, MinWidth: 960, MinHeight: 640,
			Content: ui.View(state.view),
		})
		win.OnClosed(mygo.App.Quit)
		win.Show()
		if autoClose > 0 {
			time.AfterFunc(autoClose, mygo.App.Quit)
		}
	})
	return mygo.App.Run()
}

type colors struct {
	bg, side, panel, text, muted, line, selected, blue, green, amber, red, stop ui.Color
}

func palette(dark bool) colors {
	var hex []string
	if dark {
		hex = []string{"#141416", "#1c1c1f", "#202024", "#f5f5f7", "#b6b6bf", "#38383d", "#252f42", "#9dc4ff", "#86d8af", "#f3c275", "#ffb0aa", "#a32d2a"}
	} else {
		hex = []string{"#ffffff", "#f4f4f6", "#fafafc", "#1d1d1f", "#53545d", "#dcdde3", "#e6effc", "#1956ad", "#1d6a43", "#79500b", "#a12823", "#ad2d28"}
	}
	c := make([]ui.Color, len(hex))
	for i, code := range hex {
		c[i] = ui.Hex(code)
	}
	return colors{c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7], c[8], c[9], c[10], c[11]}
}

func (a *sample) view(c *ui.Context) {
	t := *c.Theme()
	switch a.theme {
	case "light":
		t = *ui.LightTheme()
	case "dark":
		t = *ui.DarkTheme()
	}
	p := palette(t.Dark)
	t.Background, t.Text, t.TextMuted, t.Border, t.FontSize = p.bg, p.text, p.muted, p.line, 14
	t.Surface, t.Radius = p.panel, 8
	c.SetTheme(&t)
	ui.Row(c).Fill().AlignItems(ui.Stretch).Background(p.bg).Children(func() {
		a.sidebar(c, p)
		ui.Column(c).Grow(1).MinWidth(0).MinHeight(0).Children(func() {
			a.header(c, p)
			ui.Scroll(c).Grow(1).MinHeight(0).Children(func() {
				ui.Column(c).Padding(24).Gap(18).FillWidth().Children(func() {
					switch a.page {
					case "connection":
						a.connection(c, p)
					case "checks":
						a.detection(c, p)
					case "reports":
						section(c, "报告与复核", "当前样例没有持久化报告。", p)
					case "plugins":
						section(c, "插件管理", "底盘连接替身 · 0.1.0 · 开发样例", p)
					case "settings":
						section(c, "外观设置", "选择窗口使用的主题。", p)
						ui.Row(c).Gap(8).Children(func() {
							for _, item := range []struct{ id, title string }{{"system", "跟随系统"}, {"light", "浅色"}, {"dark", "深色"}} {
								button(c.Key(item.id), item.title, 36, a.theme == item.id, p).OnClick(func() {
									a.theme = item.id
									mygo.Theme.SetSource(mygo.ThemeSource(item.id))
								})
							}
						})
					}
				})
			})
			a.footer(c, p)
		})
	})
}

func text(c *ui.Context, s string, size float32, col ui.Color) ui.Element {
	return ui.Text(c, s).FontSize(size).TextColor(col)
}

func button(c *ui.Context, title string, height float32, active bool, p colors) ui.Element {
	b := ui.Button(c, title).Height(height).Radius(8)
	if active {
		b.Background(p.selected).TextColor(p.blue)
	}
	return b
}

func line(c *ui.Context, p colors) { ui.Box(c).Height(1).FillWidth().Background(p.line).Shrink(0) }

func section(c *ui.Context, title, caption string, p colors) {
	ui.Column(c).Gap(5).Children(func() {
		text(c, title, 24, p.text).FontWeight(650)
		text(c, caption, 14, p.muted)
	})
}

func (a *sample) sidebar(c *ui.Context, p colors) {
	ui.Column(c).Width(232).Shrink(0).Padding(16).Gap(12).Background(p.side).Children(func() {
		text(c, "TB VERIFY", 13, p.blue).FontWeight(750)
		ui.Column(c).Padding(12).Gap(4).Background(p.bg).Border(1, p.line).Radius(10).Children(func() {
			text(c, "底盘连接替身", 16, p.text).FontWeight(650)
			text(c, "开发包 0.1.0 · SIM-01", 12, p.muted)
		})
		ui.TextInput(c.Key("search"), &a.query).Height(36).Label("搜索检测项").Placeholder("搜索检测项").OnSubmit(func() { a.page = "checks" })
		text(c, "工作区", 12, p.muted).FontWeight(600)
		for _, item := range []struct{ id, label string }{
			{"connection", "产品与连接"}, {"checks", "检测运行"}, {"reports", "报告与复核"},
			{"plugins", "插件管理"}, {"settings", "设置"},
		} {
			button(c.Key(item.id), item.label, 38, a.page == item.id, p).FillWidth().OnClick(func() { a.page = item.id })
		}
		ui.Box(c).Grow(1)
		line(c, p)
		text(c, "技术样例 · 模拟状态", 12, p.muted)
	})
}

func (a *sample) header(c *ui.Context, p colors) {
	ui.Row(c).Height(56).Shrink(0).PaddingX(24).AlignItems(ui.Center).Justify(ui.SpaceBetween).Children(func() {
		titles := map[string]string{"connection": "产品与连接", "checks": "检测运行", "reports": "报告与复核", "plugins": "插件管理", "settings": "设置"}
		text(c, titles[a.page], 15, p.text).FontWeight(650)
		text(c, "底盘连接替身   /   SIM-01", 13, p.muted)
	})
	line(c, p)
}

func field(c *ui.Context, title string, value *string, hint string, p colors) {
	ui.Column(c.Key(title)).Gap(6).FillWidth().Children(func() {
		text(c, title, 13, p.text).FontWeight(600)
		ui.TextInput(c, value).Height(38).FillWidth().Label(title).Placeholder(hint)
	})
}

func (a *sample) connection(c *ui.Context, p colors) {
	section(c, "配置连接", "当前配置用于本机演示，所有数据均由替身提供。", p)
	ui.Row(c).Gap(18).AlignItems(ui.Start).Children(func() {
		ui.Column(c).Grow(1).MinWidth(0).Padding(18).Gap(14).Background(p.panel).Border(1, p.line).Radius(10).Children(func() {
			text(c, "设备身份与通信参数", 16, p.text).FontWeight(650)
			field(c, "设备标识", &a.device, "例如 SIM-01", p)
			field(c, "虚拟连接地址", &a.address, "例如 sim://bench-01", p)
			field(c, "操作员备注", &a.operator, "可输入中文", p)
			if a.formError != "" {
				text(c, a.formError, 13, p.red)
			}
			ui.PrimaryButton(c, "查看模拟检测").Height(38).AlignSelf(ui.Start).OnClick(func() {
				if strings.TrimSpace(a.device) == "" || !strings.HasPrefix(a.address, "sim://") || len(a.address) <= len("sim://") {
					a.formError = "请填写设备标识和有效的 sim:// 地址。"
					return
				}
				a.formError, a.page = "", "checks"
				if a.session == "idle" {
					a.session = "running"
				}
			})
		})
		ui.Column(c).Width(210).Shrink(0).Gap(10).Padding(16).Border(1, p.line).Radius(10).Children(func() {
			text(c, "底盘连接替身", 16, p.text).FontWeight(650)
			text(c, "虚拟传输 · 开发包 0.1.0", 13, p.muted)
			line(c, p)
			text(c, "检测内容", 13, p.text).FontWeight(650)
			text(c, "设备身份、样例电压、遥测和证据状态。", 13, p.muted)
		})
	})
}

func (a *sample) detection(c *ui.Context, p colors) {
	section(c, "检测工作台", "SIM-01 · 连接与预检 / 只读采集 / 记录", p)
	if a.operator != "" {
		text(c, "操作员备注："+a.operator, 14, p.muted)
	}
	ui.Row(c).Gap(16).AlignItems(ui.Start).Children(func() {
		ui.Column(c).Grow(1).MinWidth(0).Gap(4).Children(func() {
			text(c, "检测项目", 14, p.text).FontWeight(650)
			shown := 0
			for index, item := range checks {
				if !strings.Contains(item.title+item.category, strings.TrimSpace(a.query)) {
					continue
				}
				shown++
				b := ui.ButtonBase(c.Key(item.title)).Height(54).FillWidth().PaddingX(12).Radius(8).AlignItems(ui.Center).Justify(ui.SpaceBetween).Label(item.title+" "+item.verdict).Border(1, p.line)
				if a.selected == index || b.Hovered() {
					b.Background(p.selected)
				}
				b.OnClick(func() { a.selected = index }).Children(func() {
					ui.Column(c).Gap(2).Children(func() {
						text(c, item.title, 14, p.text).FontWeight(600)
						text(c, a.stepState(item), 12, p.muted)
					})
					col := p.muted
					if item.verdict == "PASS" {
						col = p.green
					} else if item.verdict == "WARN" {
						col = p.amber
					}
					text(c, item.verdict, 12, col).FontWeight(650)
				})
			}
			if shown == 0 {
				text(c, "没有匹配的检测项。", 14, p.muted)
			}
		})
		item := checks[a.selected]
		ui.Column(c).Width(300).Shrink(0).Padding(16).Gap(10).Background(p.panel).Border(1, p.line).Radius(10).Children(func() {
			text(c, item.category, 12, p.muted)
			text(c, item.title, 19, p.text).FontWeight(650)
			text(c, item.note, 13, p.muted)
			line(c, p)
			text(c, "样例测量值", 12, p.muted)
			text(c, item.measured, 18, p.text).FontWeight(650)
			text(c, "判定条件："+item.criterion, 13, p.muted)
			text(c, "证据来源：内置样例", 13, p.muted)
		})
	})
}

func (a *sample) stepState(item check) string {
	if item.verdict != "PENDING" {
		return item.state
	}
	switch a.session {
	case "stopped":
		return "未完成 · 模拟会话取消"
	case "idle":
		return "未启动"
	default:
		return item.state
	}
}

func (a *sample) footer(c *ui.Context, p colors) {
	line(c, p)
	ui.Row(c).Height(76).Shrink(0).PaddingX(24).Gap(12).Background(p.panel).AlignItems(ui.Center).Children(func() {
		label := "未启动模拟会话"
		complete := 0
		if a.session == "running" {
			label, complete = "模拟检测中", 2
		} else if a.session == "stopped" {
			label, complete = "模拟会话已取消", 2
		}
		ui.Column(c).Grow(1).MinWidth(0).Gap(5).Children(func() {
			text(c, "SIM-01 · "+label, 14, p.text).FontWeight(650)
			ui.Row(c).Gap(8).AlignItems(ui.Center).Children(func() {
				ui.Progress(c, float64(complete)/float64(len(checks))).Width(124)
				text(c, fmt.Sprintf("%d / %d 项完成", complete, len(checks)), 12, p.muted)
			})
		})
		button(c.Key("show-session"), "查看会话", 38, false, p).OnClick(func() { a.page = "checks" })
		stop := ui.Button(c.Key("stop"), "请求停止").Height(44).PaddingX(14).Disabled(a.session != "running")
		if a.session == "running" {
			stop.Background(p.stop).TextColor(ui.Hex("#ffffff")).OnClick(func() { a.session = "stopped" })
		}
	})
}
