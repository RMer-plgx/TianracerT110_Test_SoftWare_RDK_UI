package main

import (
	"flag"
	"log"
	"time"

	"github.com/tianbot/tb-verify/platform/desktop"
)

func main() {
	theme := flag.String("theme", "system", "system, light or dark")
	page := flag.String("page", "connection", "connection or checks")
	width := flag.Int("width", 1180, "initial window width")
	height := flag.Int("height", 780, "initial window height")
	exit := flag.Duration("auto-close", 0*time.Second, "close after a bounded native launch smoke")
	flag.Parse()
	if (*theme != "system" && *theme != "light" && *theme != "dark") ||
		(*page != "connection" && *page != "checks") || *width < 960 || *height < 640 || *exit < 0 {
		log.Fatal("invalid theme, page, size or auto-close")
	}
	if err := desktop.Run(*theme, *page, *width, *height, *exit); err != nil {
		log.Fatal(err)
	}
}
