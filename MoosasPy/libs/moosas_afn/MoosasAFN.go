package main

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

type airFlowNetwork struct {
	SchemaVersion int
	Levels        []levelData
	Zones         []zone
	Paths         []flowPath
}

type levelData struct {
	Nr    int     `json:"nr"`
	RefHt float64 `json:"refht"`
	DelHt float64 `json:"delht"`
	Name  string  `json:"name"`
}

type zone struct {
	Volume      float64        `json:"volume"`
	Row         int            `json:"-"`
	Col         int            `json:"-"`
	Hei         int            `json:"-"`
	Points      [][]int        `json:"-"`
	PrjName     string         `json:"prjName"`
	UserName    string         `json:"userName"`
	Heat        float64        `json:"heatLoad"`
	Temperature float64        `json:"temperature"`
	Level       float64        `json:"level"`
	LevelIndex  int            `json:"levelIndex"`
	RelHt       float64        `json:"relHt"`
	Contam      map[string]any `json:"contam"`
	PositionX   float64        `json:"position_x"`
	PositionY   float64        `json:"position_y"`
	PositionZ   float64        `json:"position_z"`
	Boundary    string         `json:"boundary"`
	PrjIndex    int            `json:"prjIndex"`
}

type flowPath struct {
	UserName   string         `json:"userName"`
	PathType   string         `json:"pathType"`
	Height     float64        `json:"pathHeight"`
	Width      float64        `json:"pathWidth"`
	Row        int            `json:"-"`
	Col        int            `json:"-"`
	Hei        int            `json:"-"`
	From       int            `json:"-"`
	To         int            `json:"-"`
	Pressure   float64        `json:"pressure"`
	WinType    int            `json:"winType"`
	Level      float64        `json:"level"`
	LevelIndex int            `json:"levelIndex"`
	RelHt      float64        `json:"relHt"`
	Contam     map[string]any `json:"contam"`
	PositionX  float64        `json:"position_x"`
	PositionY  float64        `json:"position_y"`
	PositionZ  float64        `json:"position_z"`
	PrjIndex   int            `json:"prjIndex"`
	FromZone   int            `json:"fromZone"`
	ToZone     int            `json:"toZone"`
	Element    map[string]any `json:"element"`
}

type globalInfo struct {
	networkFile string
	directory   string
	prjName     string
	resultFile  string
	simulation  bool
	split       bool
	t0          string
}

type networkPayload struct {
	SchemaVersion int                        `json:"schemaVersion"`
	Levels        []levelData                `json:"levels"`
	Zones         map[string]json.RawMessage `json:"zones"`
	Paths         map[string]json.RawMessage `json:"paths"`
}

type zonePayload struct {
	UserName    string         `json:"userName"`
	Temperature float64        `json:"temperature"`
	PrjIndex    int            `json:"prjIndex"`
	PrjName     string         `json:"prjName"`
	HeatLoad    float64        `json:"heatLoad"`
	Volume      float64        `json:"volume"`
	PositionX   float64        `json:"position_x"`
	PositionY   float64        `json:"position_y"`
	PositionZ   float64        `json:"position_z"`
	Boundary    string         `json:"boundary"`
	Level       float64        `json:"level"`
	LevelIndex  int            `json:"levelIndex"`
	RelHt       float64        `json:"relHt"`
	Contam      map[string]any `json:"contam"`
}

type pathPayload struct {
	UserName   string         `json:"userName"`
	PathType   string         `json:"pathType"`
	PrjIndex   int            `json:"prjIndex"`
	PathHeight float64        `json:"pathHeight"`
	PathWidth  float64        `json:"pathWidth"`
	PositionX  float64        `json:"position_x"`
	PositionY  float64        `json:"position_y"`
	PositionZ  float64        `json:"position_z"`
	FromZone   int            `json:"fromZone"`
	ToZone     int            `json:"toZone"`
	Pressure   float64        `json:"pressure"`
	WinType    int            `json:"winType"`
	Level      float64        `json:"level"`
	LevelIndex int            `json:"levelIndex"`
	RelHt      float64        `json:"relHt"`
	Contam     map[string]any `json:"contam"`
	Element    map[string]any `json:"element"`
}

func help() {
	fmt.Println("Moosas ContamX Builder and reader.")
	fmt.Println("Command line should be: MoosasAFN [-h,-p...] inputNetworkFile.json")
	fmt.Println("Optional command:")
	fmt.Println("-h / -help : reprint the help information")
	fmt.Println("-p / -project : base name of the prj file  (default: network)")
	fmt.Println("-d / -directory : directory where the project file and result to put  (default: execution directory)")
	fmt.Println("-o / -output : result output file path (default: execution directory/airVel.o)")
	fmt.Println("-r / -run : 1 if run contamX for all built *.prj files and gather the results (default: 0)")
	fmt.Println("-s / -split : 1 if split the input network into several networks (default: 1)")
	fmt.Println("-t / -t0 : OutdoorTemperature (default: 25)")
}

func main() {
	exePath, err := os.Executable()
	if err != nil {
		log.Fatal(err)
	}
	res, _ := filepath.EvalSymlinks(filepath.Dir(exePath))

	info := globalInfo{
		networkFile: "",
		directory:   res,
		prjName:     "network_",
		resultFile:  filepath.Join(res, "airVel.o"),
		simulation:  false,
		split:       true,
		t0:          "298.15",
	}

	for i := 1; i < len(os.Args); {
		if os.Args[i] == "-h" || os.Args[i] == "-help" {
			help()
			return
		}
		if os.Args[i] == "-p" || os.Args[i] == "-project" {
			info.prjName = os.Args[i+1]
		}
		if os.Args[i] == "-o" || os.Args[i] == "-output" {
			info.resultFile, _ = filepath.Abs(os.Args[i+1])
		}
		if os.Args[i] == "-r" || os.Args[i] == "-run" {
			info.simulation = os.Args[i+1] == "1"
		}
		if os.Args[i] == "-s" || os.Args[i] == "-split" {
			info.split = os.Args[i+1] == "1"
		}
		if os.Args[i] == "-t" || os.Args[i] == "-t0" {
			t, _ := strconv.ParseFloat(os.Args[i+1], 64)
			if t < 273.15 {
				t += 273.15
			}
			info.t0 = strconv.FormatFloat(t, 'f', 2, 64)
		}
		if os.Args[i] == "-d" || os.Args[i] == "-directory" {
			info.directory, _ = filepath.Abs(os.Args[i+1])
		}
		i++
	}

	info.networkFile, _ = filepath.Abs(os.Args[len(os.Args)-1])
	fileInfo, err := os.Lstat(info.networkFile)
	if err != nil {
		fmt.Println("invalid inputFile. Please check:", info.networkFile)
		return
	}
	if !fileInfo.Mode().IsRegular() {
		fmt.Println("inputFile flowPath is not a file. Please check:", info.networkFile)
		return
	}

	prjFilePath := make([]string, 0)
	networks := []airFlowNetwork{readFile(info.networkFile)}
	if info.split {
		networks = splitPaths(networks[0])
		for i, network := range networks {
			zoneFile := networkToFile(info.prjName+strconv.Itoa(i)+".json", info, network)
			fmt.Println("create such networkFile:", zoneFile)
			prjFile := generatePrj(info.prjName+strconv.Itoa(i)+".prj", info, network)
			prjFilePath = append(prjFilePath, prjFile)
			fmt.Println("create such prjFile:", prjFile)
		}
	} else {
		prjFile := generatePrj(info.prjName+".prj", info, networks[0])
		prjFilePath = append(prjFilePath, prjFile)
		fmt.Println("create such prjFile:", prjFile)
	}

	if info.simulation {
		runContamX(prjFilePath, networks, info.resultFile)
		fmt.Println("create such result:", info.resultFile)
	}
}

func runContamX(prjFilePath []string, networks []airFlowNetwork, resultFile string) {
	resultStr := ""
	for i := 0; i < len(networks); i++ {
		lfrFile := callContam(networks[i], prjFilePath[i])
		resultStr += outputResults(networks[i], lfrFile)
	}
	os.Remove(resultFile)
	file, _ := os.Create(resultFile)
	io.WriteString(file, resultStr)
}

func readFile(networkFile string) airFlowNetwork {
	input0, _ := os.ReadFile(networkFile)
	return readJSONNetwork(string(input0))
}

func readJSONNetwork(content string) airFlowNetwork {
	payload := map[string]any{}
	if err := json.Unmarshal([]byte(content), &payload); err != nil {
		log.Fatal(err)
	}
	schemaVersion := mapInt(payload, []string{"schemaVersion"}, 1)
	levels := decodeLevels(payload["levels"])
	zonePayloads := decodeZonePayloads(payload["zones"])
	pathPayloads := decodePathPayloads(payload["paths"])
	network := airFlowNetwork{
		SchemaVersion: schemaVersion,
		Levels:        completeLevels(levels, zonePayloads),
		Zones:         make([]zone, len(zonePayloads)),
		Paths:         make([]flowPath, len(pathPayloads)),
	}
	for i, item := range zonePayloads {
		points := parseBoundaryPoints(item.Boundary)
		levelIndex := item.LevelIndex
		if levelIndex <= 0 {
			levelIndex = inferLevelIndex(item.Level, network.Levels)
		}
		prjIndex := item.PrjIndex
		if prjIndex <= 0 {
			prjIndex = i + 1
		}
		prjName := item.PrjName
		if strings.TrimSpace(prjName) == "" {
			prjName = fmt.Sprintf("z%03d", prjIndex)
		}
		network.Zones[i] = zone{
			Volume:      item.Volume,
			Row:         mToCM(item.PositionX),
			Col:         mToCM(item.PositionY),
			Hei:         mToCM(item.PositionZ),
			Points:      points,
			PrjName:     prjName,
			UserName:    item.UserName,
			Heat:        item.HeatLoad,
			Temperature: toKelvin(item.Temperature),
			Level:       item.Level,
			LevelIndex:  levelIndex,
			RelHt:       item.RelHt,
			Contam:      ensureMap(item.Contam),
			PositionX:   item.PositionX,
			PositionY:   item.PositionY,
			PositionZ:   item.PositionZ,
			Boundary:    item.Boundary,
			PrjIndex:    prjIndex,
		}
	}
	for i, item := range pathPayloads {
		levelIndex := item.LevelIndex
		if levelIndex <= 0 {
			levelIndex = inferLevelIndex(item.Level, network.Levels)
		}
		prjIndex := item.PrjIndex
		if prjIndex <= 0 {
			prjIndex = i + 1
		}
		pathType := strings.TrimSpace(item.PathType)
		if pathType == "" {
			if dtype, ok := ensureMap(item.Element)["dtype"].(string); ok && strings.TrimSpace(strings.ToLower(dtype)) == "plr_leak3" {
				pathType = "leakage"
			} else {
				pathType = "opening"
			}
		}
		network.Paths[i] = flowPath{
			UserName:   item.UserName,
			PathType:   pathType,
			Height:     item.PathHeight,
			Width:      item.PathWidth,
			Row:        mToCM(item.PositionX),
			Col:        mToCM(item.PositionY),
			Hei:        mToCM(item.PositionZ),
			From:       oneBasedToZero(item.FromZone),
			To:         oneBasedToZero(item.ToZone),
			Pressure:   item.Pressure,
			WinType:    item.WinType,
			Level:      item.Level,
			LevelIndex: levelIndex,
			RelHt:      item.RelHt,
			Contam:     ensureMap(item.Contam),
			PositionX:  item.PositionX,
			PositionY:  item.PositionY,
			PositionZ:  item.PositionZ,
			PrjIndex:   prjIndex,
			FromZone:   item.FromZone,
			ToZone:     item.ToZone,
			Element:    ensureMap(item.Element),
		}
	}
	return network
}

func networkToFile(zonFileBaseName string, info globalInfo, network airFlowNetwork) string {
	payload := networkPayload{
		SchemaVersion: network.SchemaVersion,
		Levels:        network.Levels,
		Zones:         map[string]json.RawMessage{},
		Paths:         map[string]json.RawMessage{},
	}
	for _, z := range network.Zones {
		item := zonePayload{
			UserName:    z.UserName,
			Temperature: z.Temperature,
			PrjIndex:    z.PrjIndex,
			PrjName:     z.PrjName,
			HeatLoad:    z.Heat,
			Volume:      z.Volume,
			PositionX:   z.PositionX,
			PositionY:   z.PositionY,
			PositionZ:   z.PositionZ,
			Boundary:    z.Boundary,
			Level:       z.Level,
			LevelIndex:  z.LevelIndex,
			RelHt:       z.RelHt,
			Contam:      ensureMap(z.Contam),
		}
		raw, _ := json.Marshal(item)
		payload.Zones[z.UserName] = raw
	}
	for _, p := range network.Paths {
		item := pathPayload{
			UserName:   p.UserName,
			PathType:   p.PathType,
			PrjIndex:   p.PrjIndex,
			PathHeight: p.Height,
			PathWidth:  p.Width,
			PositionX:  p.PositionX,
			PositionY:  p.PositionY,
			PositionZ:  p.PositionZ,
			FromZone:   ifZeroBasedToOneBased(p.From),
			ToZone:     ifZeroBasedToOneBased(p.To),
			Pressure:   p.Pressure,
			WinType:    p.WinType,
			Level:      p.Level,
			LevelIndex: p.LevelIndex,
			RelHt:      p.RelHt,
			Contam:     ensureMap(p.Contam),
			Element:    ensureMap(p.Element),
		}
		raw, _ := json.Marshal(item)
		payload.Paths[p.UserName] = raw
	}
	fullPath := path.Join(info.directory, zonFileBaseName)
	os.Remove(fullPath)
	file, _ := os.Create(fullPath)
	encoder := json.NewEncoder(file)
	encoder.SetIndent("", "  ")
	_ = encoder.Encode(payload)
	file.Close()
	return fullPath
}

func generatePrj(prjBaseName string, info globalInfo, network airFlowNetwork) string {
	lines := []string{
		"ContamW 3.4.0.4 0",
		"afn",
		"! rows cols ud uf    T   uT     N     wH  u  Ao    a",
		"    58   66  0  0 " + info.t0 + " 2    0.00 10.00 0 0.600 0.280",
		"!  scale     us  orgRow  orgCol  invYaxis showGeom",
		"  1.000e+00   0      56       1     0        0",
		"! Ta       Pb      Ws    Wd    rh  day u..",
		info.t0 + " 101325.0  0.000   0.0 0.000 1 2 0 0 1 ! steady simulation",
		info.t0 + " 101325.0  1.000 270.0 0.000 1 2 0 0 1 ! wind pressure test",
		"null ! no weather file",
		"null ! no contaminant file",
		"null ! no continuous values file",
		"null ! no discrete values file",
		"null ! no WPC file",
		"null ! no EWC file",
		"WPC description",
		"!  Xref    Yref    Zref   angle u",
		"   0.000   0.000   0.000   0.00 0",
		"! epsP epsS  tShift  dStart dEnd wp mf wpctrig",
		"  0.01 0.01 00:00:00   1/1   1/1  0  0  0",
		"! latd  longtd   tznr  altd  Tgrnd u..",
		" 40.00  -90.00  -6.00     0 283.15 2 0",
		"!sim_af afcalc afmaxi afrcnvg afacnvg afrelax uac Pbldg uPb",
		"     0      1     30   1e-05   1e-06    0.75   0 50.00   0",
		"!   slae rs aflmaxi aflcnvg aflinit Tadj",
		"      0   1    100   1e-06      1    0",
		"!sim_mf slae rs maxi   relcnvg   abscnvg relax gamma ucc",
		"    0             30  1.00e-04  1.00e-15 1.250         0 ! (cyclic)",
		"          0   1  100  1.00e-06  1.00e-15 1.100 1.000   0 ! (non-trace)",
		"          0   1  100  1.00e-06  1.00e-15 1.100 1.000   0 ! (trace)",
		"          0   1  100  1.00e-06  1.00e-15 1.100         0 ! (cvode)",
		"!mf_solver sim_1dz sim_1dd   celldx  sim_vjt udx",
		"     0        1       0     1.00e-01    0     0",
		"!cvode    rcnvg     acnvg    dtmax",
		"   0     1.00e-06  1.00e-13   0.00",
		"!tsdens relax tsmaxi cnvgSS densZP stackD dodMdt",
		"   0    0.75    20     1      0      0      0",
		"!date_st time_st  date_0 time_0   date_1 time_1    t_step   t_list   t_scrn",
		"  Jan01 00:00:00  Jan01 00:00:00  Jan01 24:00:00  00:05:00 01:00:00 01:00:00",
		"!restart  date  time",
		"    0    Jan01 00:00:00",
		"!list doDlg pfsave zfsave zcsave",
		"   1     1      1      1      1",
		"!vol ach -bw cbw exp -bw age -bw",
		"  0   0   0   0   0   0   0   0",
		"!rzf rzm rz1 csm srf log",
		"  0   0   0   1   1   1",
		"!bcx dcx pfq zfq zcq",
		"  0   0   0   0   0",
		"!dens   grav",
		" 1.2041 9.8055",
		"! 0  1  2  3  4  5  6  7  8  9  10 11 12 13 14 15 <- extra[]",
		"  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0",
		"0 ! rvals:",
		"!valZ valD valC",
		"   0    0    0",
		"!cfd   cfdcnvg  var zref maxi dtcmo solv smooth   cnvgUVW     cnvgT",
		"   0  1.00e-02    0    0 1000     1    1      1  1.00e-03  1.00e-03",
		"-999",
		"0 ! contaminants:",
		"0 ! species:",
		"-999",
		fmt.Sprintf("%d ! levels plus icon data:", len(network.Levels)),
		"! #  refHt   delHt  ni  u  name",
	}
	for _, level := range network.Levels {
		lines = append(lines, fmt.Sprintf("  %d   %.0f   %.0f 0 0 0 %s",
			level.Nr, level.RefHt, level.DelHt, defaultLevelName(level.Name, level.Nr)))
	}

	lines = append(lines, []string{
		"-999",
		"0 ! day-schedules:",
		"-999",
		"0 ! week-schedules:",
		"-999",
		"0 ! wind pressure profiles:",
		"-999",
		"0 ! kinetic reactions:",
		"-999",
		"0 ! filter elements",
		"-999",
		"0 ! filters:",
		"-999",
		"0 ! source/sink elements:",
		"-999",
		strconv.Itoa(len(network.Paths)) + " ! flow elements:",
	}...)

	for i, p := range network.Paths {
		lines = append(lines, buildFlowElementLines(i+1, p)...)
	}

	lines = append(lines, []string{
		"-999",
		"0 ! duct elements:",
		"-999",
		"0 ! control super elements:",
		"-999",
		"0 ! control nodes:",
		"-999",
		"0 ! simple AHS:",
		"-999",
		strconv.Itoa(len(network.Zones)) + " ! zones:",
		"! Z#  f  s#  c#  k#  l#  relHt    Vol  T0  P0  name  clr uH uV uT uP axs cdvf <cdvfName> cfd <cfdName> <1dData:>",
	}...)
	for i, z := range network.Zones {
		nr := i + 1
		contam := ensureMap(z.Contam)
		flags := mapInt(contam, []string{"flags"}, 3)
		ps := mapInt(contam, []string{"ps"}, 0)
		pc := mapInt(contam, []string{"pc"}, 0)
		pk := mapInt(contam, []string{"pk"}, 0)
		pl := mapInt(contam, []string{"pl"}, z.LevelIndex)
		relHt := mapFloat(contam, []string{"relHt"}, z.RelHt)
		vol := mapFloat(contam, []string{"Vol", "volume"}, z.Volume)
		t0 := toKelvin(mapFloat(contam, []string{"T0", "temperature"}, z.Temperature))
		p0 := mapFloat(contam, []string{"P0"}, 0.0)
		name := mapString(contam, []string{"name"}, z.PrjName)
		color := mapInt(contam, []string{"color"}, -1)
		uHt := mapInt(contam, []string{"u_Ht"}, 0)
		uV := mapInt(contam, []string{"u_V"}, 2)
		uT := mapInt(contam, []string{"u_T"}, 0)
		uP := mapInt(contam, []string{"u_P"}, 0)
		cdaxis := mapInt(contam, []string{"cdaxis"}, 0)
		vfType := mapInt(contam, []string{"vf_type"}, 0)
		vfNodeName := mapString(contam, []string{"vf_node_name"}, "")
		cfd := mapInt(contam, []string{"cfd"}, 0)
		cfdName := mapString(contam, []string{"cfdName", "cfd_name"}, "")
		line := fmt.Sprintf("   %d  %d   %d   %d   %d   %d   %.3f    %.2f %.2f %.2f %s %d %d %d %d %d %d %d",
			nr, flags, ps, pc, pk, pl, relHt, vol, t0, p0, name, color, uHt, uV, uT, uP, cdaxis, vfType)
		if vfType != 0 {
			line += " " + defaultOptionalName(vfNodeName)
		}
		line += fmt.Sprintf(" %d", cfd)
		if cfd != 0 {
			line += " " + defaultOptionalName(cfdName)
		}
		lines = append(lines, line)
	}

	lines = append(lines, []string{
		"-999",
		"0 ! initial zone concentrations:",
		"-999",
		strconv.Itoa(len(network.Paths)) + " ! flow paths:",
		"! P#    f  n#  m#  e#  f#  w#  a#  s#  c#  l#    X       Y      relHt  mult wPset wPmod wazm Fahs Xmax Xmin icn dir clr uH uXY udP uF cdvf <cdvfName> cfd <cfdData>",
	}...)
	for i, p := range network.Paths {
		nr := i + 1
		contam := ensureMap(p.Contam)
		flags := mapInt(contam, []string{"flags"}, pathDefaultFlags(p))
		pzn, pzm := pathZoneRefs(p)
		if hasMapValue(contam, "pzn") {
			pzn = mapInt(contam, []string{"pzn"}, pzn)
		}
		if hasMapValue(contam, "pzm") {
			pzm = mapInt(contam, []string{"pzm"}, pzm)
		}
		pe := mapInt(contam, []string{"pe"}, nr)
		pf := mapInt(contam, []string{"pf"}, 0)
		pw := mapInt(contam, []string{"pw"}, 0)
		pa := mapInt(contam, []string{"pa"}, 0)
		ps := mapInt(contam, []string{"ps"}, 0)
		pc := mapInt(contam, []string{"pc"}, 0)
		pld := mapInt(contam, []string{"pld"}, p.LevelIndex)
		x := mapFloat(contam, []string{"X"}, p.PositionX)
		y := mapFloat(contam, []string{"Y"}, p.PositionY)
		relHt := mapFloat(contam, []string{"relHt"}, p.RelHt)
		mult := mapFloat(contam, []string{"mult"}, 1.0)
		wPset := mapFloat(contam, []string{"wPset"}, p.Pressure)
		wPmod := mapInt(contam, []string{"wPmod"}, 0)
		wazm := mapInt(contam, []string{"wazm"}, -1)
		fahs := mapInt(contam, []string{"Fahs"}, 0)
		xMax := mapFloat(contam, []string{"Xmax"}, 0.0)
		xMin := mapFloat(contam, []string{"Xmin"}, 0.0)
		icon := mapInt(contam, []string{"icon"}, 27)
		dir := mapInt(contam, []string{"dir"}, 1)
		color := mapInt(contam, []string{"color"}, -1)
		uHt := mapInt(contam, []string{"u_Ht"}, 0)
		uXY := mapInt(contam, []string{"u_XY"}, 0)
		udP := mapInt(contam, []string{"u_dP"}, 0)
		uF := mapInt(contam, []string{"u_F"}, 0)
		vfType := mapInt(contam, []string{"vf_type"}, 0)
		vfNodeName := mapString(contam, []string{"vf_node_name"}, "")
		cfd := mapInt(contam, []string{"cfd"}, 0)
		lineParts := []string{
			strconv.Itoa(nr),
			strconv.Itoa(flags),
			strconv.Itoa(pzn),
			strconv.Itoa(pzm),
			strconv.Itoa(pe),
			strconv.Itoa(pf),
			strconv.Itoa(pw),
			strconv.Itoa(pa),
			strconv.Itoa(ps),
			strconv.Itoa(pc),
			strconv.Itoa(pld),
			fmt.Sprintf("%.3f", x),
			fmt.Sprintf("%.3f", y),
			fmt.Sprintf("%.3f", relHt),
			fmt.Sprintf("%.3f", mult),
			fmt.Sprintf("%.3f", wPset),
			strconv.Itoa(wPmod),
			strconv.Itoa(wazm),
			strconv.Itoa(fahs),
			fmt.Sprintf("%.3f", xMax),
			fmt.Sprintf("%.3f", xMin),
			strconv.Itoa(icon),
			strconv.Itoa(dir),
			strconv.Itoa(color),
			strconv.Itoa(uHt),
			strconv.Itoa(uXY),
			strconv.Itoa(udP),
			strconv.Itoa(uF),
			strconv.Itoa(vfType),
		}
		line := "   " + strings.Join(lineParts, "   ")
		if vfType != 0 {
			line += " " + defaultOptionalName(vfNodeName)
		}
		line += fmt.Sprintf(" %d", cfd)
		lines = append(lines, line)
	}

	lines = append(lines, []string{
		"-999",
		"0 ! duct junctions:",
		"-999",
		"0 ! initial junction concentrations:",
		"-999",
		"0 ! duct segments:",
		"-999",
		"0 ! source/sinks:",
		"-999",
		"0 ! occupancy schedules:",
		"-999",
		"0 ! exposures:",
		"-999",
		"0 ! annotations:",
		"-999",
		"* end project file.",
	}...)

	var builder strings.Builder
	for _, line := range lines {
		builder.WriteString(line + "\n")
	}
	fullPath := path.Join(info.directory, prjBaseName)
	os.Remove(fullPath)
	file, _ := os.Create(fullPath)
	io.WriteString(file, builder.String())
	file.Close()
	return fullPath
}

func splitPaths(networkInput airFlowNetwork) []airFlowNetwork {
	paths := networkInput.Paths
	used, networks := make([]bool, len(paths)), []airFlowNetwork{}
	for getUnusedIndex(used) < len(paths) {
		startPoint := getUnusedIndex(used)
		zoneInNetwork := make([]bool, len(networkInput.Zones))
		subPath := []flowPath{networkInput.Paths[startPoint]}
		subZone := make([]zone, 0)

		used[startPoint] = true
		if networkInput.Paths[startPoint].From >= 0 {
			zoneInNetwork[networkInput.Paths[startPoint].From] = true
		}
		if networkInput.Paths[startPoint].To >= 0 {
			zoneInNetwork[networkInput.Paths[startPoint].To] = true
		}

		for count := 0; count < len(subPath); {
			count = len(subPath)
			for i := 0; i < len(networkInput.Paths); i++ {
				if used[i] {
					continue
				}
				fromConnected := networkInput.Paths[i].From >= 0 && zoneInNetwork[networkInput.Paths[i].From]
				toConnected := networkInput.Paths[i].To >= 0 && zoneInNetwork[networkInput.Paths[i].To]
				if fromConnected || toConnected {
					used[i] = true
					if networkInput.Paths[i].To >= 0 {
						zoneInNetwork[networkInput.Paths[i].To] = true
					}
					if networkInput.Paths[i].From >= 0 {
						zoneInNetwork[networkInput.Paths[i].From] = true
					}
					subPath = append(subPath, networkInput.Paths[i])
				}
			}
		}

		transform := make(map[int]int)
		for i := range zoneInNetwork {
			if zoneInNetwork[i] {
				subZone = append(subZone, networkInput.Zones[i])
				transform[i] = len(subZone) - 1
			}
		}
		for i := range subPath {
			if subPath[i].From >= 0 {
				if v, ok := transform[subPath[i].From]; ok {
					subPath[i].From = v
				} else {
					subPath[i].From = -1
				}
			}
			if subPath[i].To >= 0 {
				if v, ok := transform[subPath[i].To]; ok {
					subPath[i].To = v
				} else {
					subPath[i].To = -1
				}
			}
		}

		networks = append(networks, airFlowNetwork{
			SchemaVersion: networkInput.SchemaVersion,
			Levels:        cloneLevels(networkInput.Levels),
			Zones:         subZone,
			Paths:         subPath,
		})
	}
	return networks
}

func callContam(network airFlowNetwork, prjFile string) string {
	exePath, err := os.Executable()
	if err != nil {
		log.Fatal(err)
	}
	exeDir, _ := filepath.EvalSymlinks(filepath.Dir(exePath))
	simFile := path.Join(filepath.Dir(prjFile), strings.TrimSuffix(path.Base(prjFile), ".prj")+".sim")
	batFile := path.Join(filepath.Dir(prjFile), strings.TrimSuffix(path.Base(prjFile), ".prj")+".bat")
	lines := "cd " + exeDir + "\n"
	lines += "contam\\contamx3 " + prjFile + "\n"
	lines += "(echo n && echo y && echo 1-" + strconv.Itoa(len(network.Paths)) + ") | simread " + simFile + "\n"
	os.Remove(batFile)
	file, _ := os.Create(batFile)
	io.WriteString(file, lines)
	file.Close()
	exec.Command(batFile).Run()
	return path.Join(filepath.Dir(prjFile), strings.TrimSuffix(path.Base(prjFile), ".prj")+".lfr")
}

func getLevelIcons(network airFlowNetwork) map[int][][]int {
	levelIcons := make(map[int][][]int)
	rec := make(map[string]struct{})
	for _, p := range network.Paths {
		level := safeLevelIndex(p.LevelIndex)
		levelIcons[level] = append(levelIcons[level], []int{27, p.Col, p.Row, pathIconIndex(p)})
		for _, i := range []int{p.From, p.To} {
			if i == -1 || i >= len(network.Zones) {
				continue
			}
			z := network.Zones[i]
			zoneLevel := safeLevelIndex(z.LevelIndex)
			c := strconv.Itoa(z.Row) + "," + strconv.Itoa(z.Col) + "," + strconv.Itoa(zoneLevel)
			if _, ok := rec[c]; !ok {
				levelIcons[zoneLevel] = append(levelIcons[zoneLevel], []int{5, z.Col, z.Row, i + 1})
				rec[c] = struct{}{}
			}
			for _, point := range z.Points {
				c = strconv.Itoa(point[0]) + "," + strconv.Itoa(point[1]) + "," + strconv.Itoa(zoneLevel)
				if _, ok := rec[c]; ok {
					continue
				}
				levelIcons[zoneLevel] = append(levelIcons[zoneLevel], []int{16, point[1], point[0], 0})
				rec[c] = struct{}{}
			}
		}
	}
	for level := range levelIcons {
		sort.Slice(levelIcons[level], func(i, j int) bool {
			if levelIcons[level][i][2] < levelIcons[level][j][2] {
				return true
			}
			return levelIcons[level][i][1] < levelIcons[level][j][1]
		})
	}
	return levelIcons
}

func outputResults(network airFlowNetwork, lfrFilePath string) string {
	airVol := float64(0)
	airVel := ""
	airNetwork := make([][]float64, len(network.Zones)+1)
	outdoor := len(network.Zones)
	isValidZone := func(i int) bool {
		return i >= 0 && i < len(network.Zones)
	}
	for i := range airNetwork {
		airNetwork[i] = make([]float64, len(network.Zones)+1)
	}

	lfrFile, _ := os.ReadFile(lfrFilePath)
	lfrData := strings.Split(string(lfrFile), "\r\n")
	for i, p := range network.Paths {
		if i+1 >= len(lfrData) {
			continue
		}
		lfrRow := getLfrRow(strings.Split(lfrData[i+1], " "))
		if len(lfrRow) < 3 || len(lfrRow[2]) == 0 {
			continue
		}
		flow, _ := strconv.ParseFloat(lfrRow[2][:len(lfrRow[2])-1], 64)
		if p.From == -1 && isValidZone(p.To) {
			if flow > 0 {
				airVol += flow / 1.205 * 3600
			}
			airNetwork[outdoor][p.To] += flow / 1.205 * 3600
			airNetwork[p.To][outdoor] -= flow / 1.205 * 3600
		} else if p.To == -1 && isValidZone(p.From) {
			airNetwork[p.From][outdoor] += flow / 1.205 * 3600
			airNetwork[outdoor][p.From] -= flow / 1.205 * 3600
		} else if isValidZone(p.From) && isValidZone(p.To) {
			airVel += p.UserName + "," + network.Zones[p.From].UserName + "," + network.Zones[p.To].UserName + "," + fmt.Sprintf("%.2f", p.Height*p.Width) + "," + fmt.Sprintf("%.2f", flow/(1.205*p.Height*p.Width)) + "\n"
			airNetwork[p.From][p.To] += flow / 1.205 * 3600
			airNetwork[p.To][p.From] -= flow / 1.205 * 3600
		}
	}
	resultStr := ""
	resultStr += "!TOTAL AIR CHANGE COEFFICIENT (AIR CHANGE PER HOUR, ACH)\n"
	resultStr += fmt.Sprintf("%.2f", airVol)
	resultStr += "!Path AIR FLOW (AIR VELOCITY, m/s)\n"
	resultStr += "!pathName,fromZone,toZone,Volume(m3),Velocity(m/s)\n"
	if len(airVel) > 0 {
		resultStr += airVel[:len(airVel)-1]
	}
	resultStr += "!ZONE AIR FLOW NETWORK (AIR CHANGE PER HOUR, ACH)\n"
	resultStr += "!ZONE NAME,\t"
	for _, z := range network.Zones {
		resultStr += "," + z.UserName
	}
	resultStr += ",outdoor(amt)\n"
	resultStr += "!\t,FROM\\TO"
	for _, z := range network.Zones {
		resultStr += "," + z.PrjName
	}
	resultStr += ",outdoor(amt)\n"
	for i := range network.Zones {
		resultStr += network.Zones[i].UserName + "," + network.Zones[i].PrjName
		for j := range airNetwork[i] {
			resultStr += "," + fmt.Sprintf("%.2f", airNetwork[i][j])
		}
		resultStr += "\n"
	}
	resultStr += "\t,outdoor(amt)"
	for j := range airNetwork[len(network.Zones)] {
		resultStr += "," + fmt.Sprintf("%.2f", airNetwork[len(network.Zones)][j])
	}
	resultStr += "\n"
	return resultStr
}

func mToCMString(char string) int {
	data, _ := strconv.ParseFloat(char, 64)
	return int(math.Floor(data * 100))
}

func mToCM(value float64) int {
	return int(math.Floor(value * 100))
}

func toKelvin(value float64) float64 {
	if value < 273.15 {
		return value + 273.15
	}
	return value
}

func getUnusedIndex(used []bool) int {
	for i := 0; i < len(used); i++ {
		if !used[i] {
			return i
		}
	}
	return len(used)
}

func getLfrRow(strs []string) []string {
	res := []string{}
	for _, v := range strs {
		if len(v) > 4 {
			res = append(res, v)
		}
	}
	return res
}

func parseBoundaryPoints(boundary string) [][]int {
	points := [][]int{}
	pointstring := strings.Fields(boundary)
	for i := 0; i+1 < len(pointstring); i += 2 {
		points = append(points, []int{mToCMString(pointstring[i]), mToCMString(pointstring[i+1])})
	}
	return points
}

func ensureMap(value map[string]any) map[string]any {
	if value == nil {
		return map[string]any{}
	}
	return value
}

func cloneMap(src map[string]any) map[string]any {
	if src == nil {
		return map[string]any{}
	}
	dst := make(map[string]any, len(src))
	for k, v := range src {
		dst[k] = v
	}
	return dst
}

func toFloat(value any) (float64, bool) {
	switch v := value.(type) {
	case float64:
		return v, true
	case float32:
		return float64(v), true
	case int:
		return float64(v), true
	case int64:
		return float64(v), true
	case int32:
		return float64(v), true
	case json.Number:
		f, err := v.Float64()
		if err == nil {
			return f, true
		}
		return 0, false
	default:
		return 0, false
	}
}

func decodeLevels(raw any) []levelData {
	if raw == nil {
		return nil
	}
	levelRows, ok := raw.([]any)
	if !ok {
		return nil
	}
	levels := make([]levelData, 0, len(levelRows))
	for idx, row := range levelRows {
		item, ok := row.(map[string]any)
		if !ok {
			continue
		}
		nr := mapInt(item, []string{"nr"}, idx+1)
		levels = append(levels, levelData{
			Nr:    nr,
			RefHt: mapFloat(item, []string{"refht"}, 0.0),
			DelHt: mapFloat(item, []string{"delht"}, 3.0),
			Name:  defaultLevelName(mapString(item, []string{"name"}, ""), nr),
		})
	}
	return levels
}

func decodeZonePayloads(raw any) []zonePayload {
	items := make([]zonePayload, 0)
	switch typed := raw.(type) {
	case map[string]any:
		for key, value := range typed {
			item, ok := value.(map[string]any)
			if !ok {
				continue
			}
			userName := mapString(item, []string{"userName"}, key)
			items = append(items, zonePayload{
				UserName:    userName,
				Temperature: mapFloat(item, []string{"temperature"}, 27.0),
				PrjIndex:    mapInt(item, []string{"prjIndex"}, 0),
				PrjName:     mapString(item, []string{"prjName"}, ""),
				HeatLoad:    mapFloat(item, []string{"heatLoad"}, 0.0),
				Volume:      mapFloat(item, []string{"volume"}, 0.0),
				PositionX:   mapFloat(item, []string{"position_x"}, 0.0),
				PositionY:   mapFloat(item, []string{"position_y"}, 0.0),
				PositionZ:   mapFloat(item, []string{"position_z"}, 0.0),
				Boundary:    mapString(item, []string{"boundary"}, ""),
				Level:       mapFloat(item, []string{"level"}, 0.0),
				LevelIndex:  mapInt(item, []string{"levelIndex"}, 1),
				RelHt:       mapFloat(item, []string{"relHt"}, 0.0),
				Contam:      mapAny(item, []string{"contam"}),
			})
		}
	case []any:
		for _, value := range typed {
			item, ok := value.(map[string]any)
			if !ok {
				continue
			}
			items = append(items, zonePayload{
				UserName:    mapString(item, []string{"userName"}, ""),
				Temperature: mapFloat(item, []string{"temperature"}, 27.0),
				PrjIndex:    mapInt(item, []string{"prjIndex"}, 0),
				PrjName:     mapString(item, []string{"prjName"}, ""),
				HeatLoad:    mapFloat(item, []string{"heatLoad"}, 0.0),
				Volume:      mapFloat(item, []string{"volume"}, 0.0),
				PositionX:   mapFloat(item, []string{"position_x"}, 0.0),
				PositionY:   mapFloat(item, []string{"position_y"}, 0.0),
				PositionZ:   mapFloat(item, []string{"position_z"}, 0.0),
				Boundary:    mapString(item, []string{"boundary"}, ""),
				Level:       mapFloat(item, []string{"level"}, 0.0),
				LevelIndex:  mapInt(item, []string{"levelIndex"}, 1),
				RelHt:       mapFloat(item, []string{"relHt"}, 0.0),
				Contam:      mapAny(item, []string{"contam"}),
			})
		}
	}
	sort.SliceStable(items, func(i, j int) bool {
		left := items[i].PrjIndex
		right := items[j].PrjIndex
		if left == right {
			return items[i].UserName < items[j].UserName
		}
		if left <= 0 {
			return false
		}
		if right <= 0 {
			return true
		}
		return left < right
	})
	return items
}

func decodePathPayloads(raw any) []pathPayload {
	items := make([]pathPayload, 0)
	switch typed := raw.(type) {
	case map[string]any:
		for key, value := range typed {
			item, ok := value.(map[string]any)
			if !ok {
				continue
			}
			userName := mapString(item, []string{"userName"}, key)
			items = append(items, pathPayload{
				UserName:   userName,
				PathType:   mapString(item, []string{"pathType"}, ""),
				PrjIndex:   mapInt(item, []string{"prjIndex"}, 0),
				PathHeight: mapFloat(item, []string{"pathHeight"}, 0.0),
				PathWidth:  mapFloat(item, []string{"pathWidth"}, 0.0),
				PositionX:  mapFloat(item, []string{"position_x"}, 0.0),
				PositionY:  mapFloat(item, []string{"position_y"}, 0.0),
				PositionZ:  mapFloat(item, []string{"position_z"}, 0.0),
				FromZone:   mapInt(item, []string{"fromZone"}, -1),
				ToZone:     mapInt(item, []string{"toZone"}, -1),
				Pressure:   mapFloat(item, []string{"pressure"}, 0.0),
				WinType:    mapInt(item, []string{"winType"}, 1),
				Level:      mapFloat(item, []string{"level"}, 0.0),
				LevelIndex: mapInt(item, []string{"levelIndex"}, 1),
				RelHt:      mapFloat(item, []string{"relHt"}, 0.0),
				Contam:     mapAny(item, []string{"contam"}),
				Element:    mapAny(item, []string{"element"}),
			})
		}
	case []any:
		for _, value := range typed {
			item, ok := value.(map[string]any)
			if !ok {
				continue
			}
			items = append(items, pathPayload{
				UserName:   mapString(item, []string{"userName"}, ""),
				PathType:   mapString(item, []string{"pathType"}, ""),
				PrjIndex:   mapInt(item, []string{"prjIndex"}, 0),
				PathHeight: mapFloat(item, []string{"pathHeight"}, 0.0),
				PathWidth:  mapFloat(item, []string{"pathWidth"}, 0.0),
				PositionX:  mapFloat(item, []string{"position_x"}, 0.0),
				PositionY:  mapFloat(item, []string{"position_y"}, 0.0),
				PositionZ:  mapFloat(item, []string{"position_z"}, 0.0),
				FromZone:   mapInt(item, []string{"fromZone"}, -1),
				ToZone:     mapInt(item, []string{"toZone"}, -1),
				Pressure:   mapFloat(item, []string{"pressure"}, 0.0),
				WinType:    mapInt(item, []string{"winType"}, 1),
				Level:      mapFloat(item, []string{"level"}, 0.0),
				LevelIndex: mapInt(item, []string{"levelIndex"}, 1),
				RelHt:      mapFloat(item, []string{"relHt"}, 0.0),
				Contam:     mapAny(item, []string{"contam"}),
				Element:    mapAny(item, []string{"element"}),
			})
		}
	}
	sort.SliceStable(items, func(i, j int) bool {
		left := items[i].PrjIndex
		right := items[j].PrjIndex
		if left == right {
			return items[i].UserName < items[j].UserName
		}
		if left <= 0 {
			return false
		}
		if right <= 0 {
			return true
		}
		return left < right
	})
	return items
}

func completeLevels(levels []levelData, zones []zonePayload) []levelData {
	if len(levels) == 0 {
		values := make([]float64, 0, len(zones))
		for _, z := range zones {
			values = append(values, z.Level)
		}
		return buildLevels(values)
	}
	out := cloneLevels(levels)
	sort.Slice(out, func(i, j int) bool { return out[i].RefHt < out[j].RefHt })
	for i := range out {
		if out[i].Nr <= 0 {
			out[i].Nr = i + 1
		}
		if strings.TrimSpace(out[i].Name) == "" {
			out[i].Name = fmt.Sprintf("<%d>", out[i].Nr)
		}
		if i < len(out)-1 {
			out[i].DelHt = out[i+1].RefHt - out[i].RefHt
		} else if len(out) > 1 && out[i].DelHt == 0 {
			out[i].DelHt = out[i-1].DelHt
		} else if out[i].DelHt == 0 {
			out[i].DelHt = 3.0
		}
	}
	return out
}

func buildLevels(values []float64) []levelData {
	if len(values) == 0 {
		return []levelData{{Nr: 1, RefHt: 0.0, DelHt: 3.0, Name: "<1>"}}
	}
	uniqMap := make(map[string]float64)
	for _, value := range values {
		key := fmt.Sprintf("%.6f", value)
		uniqMap[key] = value
	}
	uniq := make([]float64, 0, len(uniqMap))
	for _, value := range uniqMap {
		uniq = append(uniq, value)
	}
	sort.Float64s(uniq)
	levels := make([]levelData, len(uniq))
	for i, ref := range uniq {
		del := 3.0
		if i < len(uniq)-1 {
			del = uniq[i+1] - ref
		} else if len(uniq) > 1 {
			del = uniq[len(uniq)-1] - uniq[len(uniq)-2]
		}
		if del == 0 {
			del = 3.0
		}
		levels[i] = levelData{Nr: i + 1, RefHt: ref, DelHt: del, Name: fmt.Sprintf("<%d>", i+1)}
	}
	return levels
}

func inferLevelIndex(level float64, levels []levelData) int {
	for _, item := range levels {
		if math.Abs(item.RefHt-level) < 1e-6 {
			return item.Nr
		}
	}
	return 1
}

func oneBasedToZero(value int) int {
	if value <= -1 {
		return -1
	}
	return value - 1
}

func ifZeroBasedToOneBased(value int) int {
	if value <= -1 {
		return -1
	}
	return value + 1
}

func pathOperable(element map[string]any) float64 {
	return math.Max(0.0, mapFloat(element, []string{"operable"}, 1.0))
}

func defaultPathElement(p flowPath, nr int) map[string]any {
	element := ensureMap(p.Element)
	op := pathOperable(element)
	h := math.Max(0.0, p.Height)
	w := math.Max(0.0, p.Width)
	isLeakage := strings.EqualFold(strings.TrimSpace(p.PathType), "leakage")
	_, autoGenerated := element["_autoGenerated"].(bool)
	scaleOperable := !isLeakage && autoGenerated
	if scaleOperable {
		h *= op
		w *= op
	}
	denom := h + w
	lam := 0.0
	if denom > 1e-12 {
		lam = h * w * h * w / denom * 0.02028
	}
	turb := h * w * 0.551543
	dtype := mapString(element, []string{"dtype"}, "")
	if strings.TrimSpace(dtype) == "" {
		if strings.EqualFold(strings.TrimSpace(p.PathType), "leakage") {
			dtype = "plr_leak3"
		} else if p.WinType == 1 {
			dtype = "dor_pl2"
		} else {
			dtype = "plr_conn"
		}
	}
	name := mapString(element, []string{"name"}, "")
	if strings.TrimSpace(name) == "" {
		name = fmt.Sprintf("p%d", nr)
	}
	result := map[string]any{
		"icon":     mapInt(element, []string{"icon"}, 27),
		"dtype":    strings.TrimSpace(dtype),
		"name":     name,
		"desc":     mapString(element, []string{"desc"}, ""),
		"operable": op,
		"lam":      mapFloat(element, []string{"lam"}, lam),
		"turb":     mapFloat(element, []string{"turb"}, turb),
		"expt": mapFloat(element, []string{"expt"}, func() float64 {
			if strings.EqualFold(strings.TrimSpace(dtype), "plr_leak3") {
				return 0.65
			}
			return 0.5
		}()),
	}
	switch result["dtype"] {
	case "dor_pl2":
		result["dH"] = mapFloat(element, []string{"dH"}, h*0.222222)
		result["ht"] = mapFloat(element, []string{"ht"}, h)
		result["wd"] = mapFloat(element, []string{"wd"}, w)
		result["cd"] = mapFloat(element, []string{"cd"}, 0.78)
		result["u_H"] = mapInt(element, []string{"u_H"}, 0)
		result["u_W"] = mapInt(element, []string{"u_W"}, 0)
	case "plr_leak1", "plr_leak2", "plr_leak3":
		result["coef"] = mapFloat(element, []string{"coef"}, 1.0)
		result["pres"] = mapFloat(element, []string{"pres"}, 4.0)
		result["area1"] = mapFloat(element, []string{"area1"}, 0.0)
		result["area2"] = mapFloat(element, []string{"area2"}, 0.0)
		defaultArea := mapFloat(element, []string{"area3"}, 1e-5)
		result["area3"] = defaultArea
		result["u_A1"] = mapInt(element, []string{"u_A1"}, 0)
		result["u_A2"] = mapInt(element, []string{"u_A2"}, 0)
		result["u_A3"] = mapInt(element, []string{"u_A3"}, 0)
		result["u_dP"] = mapInt(element, []string{"u_dP"}, 0)
	case "plr_conn":
		result["area"] = mapFloat(element, []string{"area"}, h*w)
		result["coef"] = mapFloat(element, []string{"coef"}, 0.65)
		result["u_A"] = mapInt(element, []string{"u_A"}, 0)
	}
	for key, value := range element {
		result[key] = value
	}
	if scaleOperable {
		switch result["dtype"] {
		case "dor_pl2":
			result["dH"] = h * 0.222222
			result["ht"] = h
			result["wd"] = w
			result["cd"] = mapFloat(element, []string{"cd"}, 0.78)
			result["lam"] = lam
			result["turb"] = turb
		case "plr_conn":
			result["area"] = h * w
			result["lam"] = lam
			result["turb"] = turb
		}
	}
	return result
}

func buildFlowElementLines(nr int, p flowPath) []string {
	element := defaultPathElement(p, nr)
	dtype := strings.TrimSpace(mapString(element, []string{"dtype"}, "plr_conn"))
	icon := mapInt(element, []string{"icon"}, 27)
	name := mapString(element, []string{"name"}, fmt.Sprintf("p%d", nr))
	desc := mapString(element, []string{"desc"}, "")
	lines := []string{
		fmt.Sprintf("%d %d %s %s", nr, icon, dtype, name),
		desc,
	}
	switch dtype {
	case "plr_orfc":
		lines = append(lines, formatElementLine(element, []string{"lam", "turb", "expt", "area", "dia", "coef", "Re", "u_A", "u_D"}))
	case "plr_leak1", "plr_leak2", "plr_leak3":
		lineElement := cloneMap(element)
		h := math.Max(0.0, p.Height)
		w := math.Max(0.0, p.Width)
		if area3, ok := lineElement["area3"]; ok {
			if v, ok := toFloat(area3); ok {
				lineElement["area3"] = v * h * w
			}
		}
		if area1, ok := lineElement["area1"]; ok {
			if v, ok := toFloat(area1); ok {
				lineElement["area1"] = v * h * w
			}
		}
		if area2, ok := lineElement["area2"]; ok {
			if v, ok := toFloat(area2); ok {
				lineElement["area2"] = v * h * w
			}
		}
		lines = append(lines, formatElementLine(lineElement, []string{"lam", "turb", "expt", "coef", "pres", "area1", "area2", "area3", "u_A1", "u_A2", "u_A3", "u_dP"}))
	case "plr_conn":
		lines = append(lines, formatElementLine(element, []string{"lam", "turb", "expt", "area", "coef", "u_A"}))
	case "plr_qcn", "plr_fcn":
		lines = append(lines, formatElementLine(element, []string{"lam", "turb", "expt"}))
	case "plr_test1":
		lines = append(lines, formatElementLine(element, []string{"lam", "turb", "expt", "dP", "Flow", "u_P", "u_F"}))
	case "plr_test2":
		lines = append(lines, formatElementLine(element, []string{"lam", "turb", "expt", "dP1", "F1", "dP2", "F2", "u_P1", "u_F1", "u_P2", "u_F2"}))
	case "plr_crack":
		lines = append(lines, formatElementLine(element, []string{"lam", "turb", "expt", "length", "width", "u_L", "u_W"}))
	case "plr_stair":
		lines = append(lines, formatElementLine(element, []string{"lam", "turb", "expt", "Ht", "Area", "peo", "tread", "u_A", "u_D"}))
	case "plr_shaft":
		lines = append(lines, formatElementLine(element, []string{"lam", "turb", "expt", "Ht", "area", "perim", "rough", "u_A", "u_D", "u_P", "u_R"}))
	case "plr_bdq", "plr_bdf":
		lines = append(lines, formatElementLine(element, []string{"lam", "Cp", "xp", "Cn", "xn"}))
	case "qfr_qab", "qfr_fab":
		lines = append(lines, formatElementLine(element, []string{"a", "b"}))
	case "qfr_crack":
		lines = append(lines, formatElementLine(element, []string{"a", "b", "length", "width", "depth", "nB", "u_L", "u_W", "u_D"}))
	case "qfr_test2":
		lines = append(lines, formatElementLine(element, []string{"a", "b", "dP1", "F1", "dP2", "F2", "u_P1", "u_F1", "u_P2", "u_F2"}))
	case "dor_door":
		lines = append(lines, formatElementLine(element, []string{"lam", "turb", "expt", "dTmin", "ht", "wd", "cd", "u_T", "u_H", "u_W"}))
	case "dor_pl2":
		lines = append(lines, formatElementLine(element, []string{"lam", "turb", "expt", "dH", "ht", "wd", "cd", "u_H", "u_W"}))
	case "fan_cmf", "fan_cvf":
		lines = append(lines, formatElementLine(element, []string{"Flow", "u_F"}))
	case "fan_fan":
		lines = append(lines, formatElementLine(element, []string{"lam", "turb", "expt", "rdens", "fdf", "sop", "off"}))
		fpc := mapFloatSlice(element, []string{"fpc"}, 4)
		npts := len(mapSliceOfMap(element, []string{"points"}))
		line2 := []string{
			formatFloat(fpc[0]), formatFloat(fpc[1]), formatFloat(fpc[2]), formatFloat(fpc[3]),
			strconv.Itoa(mapInt(element, []string{"npts"}, npts)),
			formatFloat(mapFloat(element, []string{"Sarea"}, 0.0)),
			strconv.Itoa(mapInt(element, []string{"u_Sa"}, 0)),
		}
		lines = append(lines, " "+strings.Join(line2, " "))
		for _, point := range mapSliceOfMap(element, []string{"points"}) {
			lines = append(lines, formatElementLine(point, []string{"mF", "u_mF", "dP", "u_dP", "rP", "u_rP"}))
		}
	case "csf_fsp", "csf_qsp", "csf_psf", "csf_psq":
		points := mapSliceOfMap(element, []string{"points"})
		lines = append(lines, fmt.Sprintf(" %d %d %d",
			mapInt(element, []string{"npts"}, len(points)),
			mapInt(element, []string{"u_x"}, 0),
			mapInt(element, []string{"u_y"}, 0),
		))
		for _, point := range points {
			lines = append(lines, formatElementLine(point, []string{"x", "y"}))
		}
	case "sup_afe":
		subElements := mapSliceOfMap(element, []string{"subElements"})
		lines = append(lines, fmt.Sprintf(" %d %d %d",
			mapInt(element, []string{"nse"}, len(subElements)),
			mapInt(element, []string{"sched"}, 0),
			mapInt(element, []string{"u_H"}, 0),
		))
		for _, sub := range subElements {
			lines = append(lines, formatElementLine(sub, []string{"nr", "relHt", "filt"}))
		}
	default:
		lines = append(lines, formatElementLine(element, []string{"lam", "turb", "expt"}))
	}
	return lines
}

func formatElementLine(data map[string]any, keys []string) string {
	values := make([]string, 0, len(keys))
	for _, key := range keys {
		if looksLikeIntField(key) {
			values = append(values, strconv.Itoa(mapInt(data, []string{key}, 0)))
		} else {
			values = append(values, formatFloat(mapFloat(data, []string{key}, 0.0)))
		}
	}
	return " " + strings.Join(values, " ")
}

func looksLikeIntField(key string) bool {
	switch key {
	case "u_A", "u_D", "u_A1", "u_A2", "u_A3", "u_dP", "u_P", "u_F", "u_L", "u_W", "u_R",
		"u_T", "u_H", "u_P1", "u_F1", "u_P2", "u_F2", "u_Sa", "u_x", "u_y",
		"npts", "nB", "tread", "nse", "sched", "nr", "filt":
		return true
	}
	return false
}

func formatFloat(value float64) string {
	return strconv.FormatFloat(value, 'f', 6, 64)
}

func cloneLevels(levels []levelData) []levelData {
	out := make([]levelData, len(levels))
	copy(out, levels)
	return out
}

func pathDefaultFlags(p flowPath) int {
	if p.From == -1 || p.To == -1 {
		return 1
	}
	return 0
}

func pathZoneRefs(p flowPath) (int, int) {
	if p.From == -1 {
		return -1, p.To + 1
	}
	if p.To == -1 {
		return -1, p.From + 1
	}
	return p.From + 1, p.To + 1
}

func hasMapValue(data map[string]any, key string) bool {
	if data == nil {
		return false
	}
	value, ok := data[key]
	if !ok {
		return false
	}
	if text, ok := value.(string); ok {
		return strings.TrimSpace(text) != ""
	}
	return true
}

func mapInt(data map[string]any, keys []string, defaultValue int) int {
	for _, key := range keys {
		if data == nil {
			break
		}
		value, ok := data[key]
		if !ok || value == nil {
			continue
		}
		switch v := value.(type) {
		case float64:
			return int(math.Round(v))
		case int:
			return v
		case string:
			if strings.TrimSpace(v) == "" {
				continue
			}
			if parsed, err := strconv.Atoi(v); err == nil {
				return parsed
			}
			if parsed, err := strconv.ParseFloat(v, 64); err == nil {
				return int(math.Round(parsed))
			}
		}
	}
	return defaultValue
}

func mapFloat(data map[string]any, keys []string, defaultValue float64) float64 {
	for _, key := range keys {
		if data == nil {
			break
		}
		value, ok := data[key]
		if !ok || value == nil {
			continue
		}
		switch v := value.(type) {
		case float64:
			return v
		case int:
			return float64(v)
		case string:
			if strings.TrimSpace(v) == "" {
				continue
			}
			if parsed, err := strconv.ParseFloat(v, 64); err == nil {
				return parsed
			}
		}
	}
	return defaultValue
}

func mapAny(data map[string]any, keys []string) map[string]any {
	for _, key := range keys {
		if data == nil {
			break
		}
		value, ok := data[key]
		if !ok || value == nil {
			continue
		}
		if result, ok := value.(map[string]any); ok {
			return result
		}
	}
	return map[string]any{}
}

func mapSliceOfMap(data map[string]any, keys []string) []map[string]any {
	for _, key := range keys {
		if data == nil {
			break
		}
		value, ok := data[key]
		if !ok || value == nil {
			continue
		}
		rows, ok := value.([]any)
		if !ok {
			continue
		}
		result := make([]map[string]any, 0, len(rows))
		for _, row := range rows {
			if item, ok := row.(map[string]any); ok {
				result = append(result, item)
			}
		}
		return result
	}
	return []map[string]any{}
}

func mapFloatSlice(data map[string]any, keys []string, size int) []float64 {
	result := make([]float64, size)
	for _, key := range keys {
		if data == nil {
			break
		}
		value, ok := data[key]
		if !ok || value == nil {
			continue
		}
		rows, ok := value.([]any)
		if !ok {
			continue
		}
		for i := 0; i < size && i < len(rows); i++ {
			switch v := rows[i].(type) {
			case float64:
				result[i] = v
			case int:
				result[i] = float64(v)
			case string:
				if parsed, err := strconv.ParseFloat(strings.TrimSpace(v), 64); err == nil {
					result[i] = parsed
				}
			}
		}
		break
	}
	return result
}

func mapString(data map[string]any, keys []string, defaultValue string) string {
	for _, key := range keys {
		if data == nil {
			break
		}
		value, ok := data[key]
		if !ok || value == nil {
			continue
		}
		text := fmt.Sprintf("%v", value)
		if strings.TrimSpace(text) != "" {
			return text
		}
	}
	return defaultValue
}

func defaultLevelName(name string, nr int) string {
	if strings.TrimSpace(name) == "" {
		return fmt.Sprintf("<%d>", nr)
	}
	return name
}

func defaultOptionalName(name string) string {
	if strings.TrimSpace(name) == "" {
		return "<null>"
	}
	return name
}

func safeLevelIndex(value int) int {
	if value <= 0 {
		return 1
	}
	return value
}

func pathIconIndex(p flowPath) int {
	if p.PrjIndex > 0 {
		return p.PrjIndex
	}
	return 0
}
