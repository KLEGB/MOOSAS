package main

import (
	"bufio"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
)

const (
	boundaryTolerance  = 1e-2
	minimumRayDistance = 1e-2
	verticalNormalZ    = 1e-6
	parallelTolerance  = 1e-12
)

type vec struct{ x, y, z float64 }
type vec2 struct{ x, y float64 }
type ray struct{ origin, direction vec }
type bounds2 struct{ minX, minY, maxX, maxY float64 }
type bounds3 struct{ minX, minY, minZ, maxX, maxY, maxZ float64 }
type ring struct {
	points        []vec2
	bounds        bounds2
	boundaryCross []float64
}
type face struct {
	factor vec
	planeD float64
	outer  ring
	holes  []ring
	useXY  bool
	u, v   vec
	bounds bounds3
}
type candidate struct {
	distance  float64
	faceIndex int
}

func dot(a, b vec) float64       { return a.x*b.x + a.y*b.y + a.z*b.z }
func add(a, b vec) vec           { return vec{a.x + b.x, a.y + b.y, a.z + b.z} }
func sub(a, b vec) vec           { return vec{a.x - b.x, a.y - b.y, a.z - b.z} }
func multi(s float64, a vec) vec { return vec{s * a.x, s * a.y, s * a.z} }
func negative(a vec) vec         { return multi(-1, a) }
func cross(a, b vec) vec         { return vec{a.y*b.z - a.z*b.y, a.z*b.x - a.x*b.z, a.x*b.y - a.y*b.x} }
func length(a vec) float64       { return math.Sqrt(dot(a, a)) }
func unit(a vec) vec {
	n := length(a)
	if n == 0 || !isFinite(n) {
		return vec{}
	}
	return multi(1/n, a)
}
func isFinite(x float64) bool { return !math.IsNaN(x) && !math.IsInf(x, 0) }

func parseVec(parts []string) (vec, error) {
	if len(parts) < 4 {
		return vec{}, fmt.Errorf("expected three coordinates")
	}
	x, e := strconv.ParseFloat(strings.TrimSpace(parts[1]), 64)
	if e != nil {
		return vec{}, e
	}
	y, e := strconv.ParseFloat(strings.TrimSpace(parts[2]), 64)
	if e != nil {
		return vec{}, e
	}
	z, e := strconv.ParseFloat(strings.TrimSpace(parts[3]), 64)
	if e != nil {
		return vec{}, e
	}
	if !isFinite(x) || !isFinite(y) || !isFinite(z) {
		return vec{}, fmt.Errorf("non-finite coordinate")
	}
	return vec{x, y, z}, nil
}

func loadRay(path string) ([]ray, error) {
	f, e := os.Open(path)
	if e != nil {
		return nil, e
	}
	defer f.Close()
	rays := make([]ray, 0)
	s := bufio.NewScanner(f)
	s.Buffer(make([]byte, 4096), 4*1024*1024)
	line := 0
	for s.Scan() {
		line++
		text := strings.TrimSpace(s.Text())
		if text == "" {
			continue
		}
		p := strings.Split(text, ",")
		if len(p) < 6 {
			return nil, fmt.Errorf("ray line %d: expected six values", line)
		}
		vals := make([]float64, 6)
		for i := range vals {
			v, err := strconv.ParseFloat(strings.TrimSpace(p[i]), 64)
			if err != nil || !isFinite(v) {
				return nil, fmt.Errorf("ray line %d: invalid value", line)
			}
			vals[i] = v
		}
		d := unit(vec{vals[3], vals[4], vals[5]})
		if length(d) == 0 {
			return nil, fmt.Errorf("ray line %d: zero direction", line)
		}
		rays = append(rays, ray{vec{vals[0], vals[1], vals[2]}, d})
	}
	if e = s.Err(); e != nil {
		return nil, e
	}
	return rays, nil
}

func projectPoint(p vec, f face) vec2 {
	if f.useXY {
		return vec2{p.x, p.y}
	}
	return vec2{dot(p, f.u), dot(p, f.v)}
}
func buildRing(points []vec, f face) (ring, error) {
	clean := make([]vec, 0, len(points))
	for _, p := range points {
		if len(clean) == 0 || length(sub(p, clean[len(clean)-1])) > 1e-12 {
			clean = append(clean, p)
		}
	}
	if len(clean) > 1 && length(sub(clean[0], clean[len(clean)-1])) <= 1e-12 {
		clean = clean[:len(clean)-1]
	}
	if len(clean) < 3 {
		return ring{}, fmt.Errorf("ring has fewer than three distinct vertices")
	}
	r := ring{points: make([]vec2, len(clean))}
	for i, p := range clean {
		q := projectPoint(p, f)
		r.points[i] = q
		if i == 0 {
			r.bounds = bounds2{q.x, q.y, q.x, q.y}
		} else {
			r.bounds.minX = math.Min(r.bounds.minX, q.x)
			r.bounds.minY = math.Min(r.bounds.minY, q.y)
			r.bounds.maxX = math.Max(r.bounds.maxX, q.x)
			r.bounds.maxY = math.Max(r.bounds.maxY, q.y)
		}
	}
	area2 := 0.0
	for i, p := range r.points {
		q := r.points[(i+1)%len(r.points)]
		area2 += p.x*q.y - q.x*p.y
	}
	if math.Abs(area2) <= 1e-12 {
		return ring{}, fmt.Errorf("ring has zero projected area")
	}
	r.boundaryCross = make([]float64, len(r.points))
	for i, a := range r.points {
		b := r.points[(i+1)%len(r.points)]
		r.boundaryCross[i] = boundaryTolerance * math.Hypot(b.x-a.x, b.y-a.y)
	}
	return r, nil
}

func prepareFace(outer []vec, holes map[int][]vec, normal vec) (face, error) {
	if len(outer) < 3 {
		return face{}, fmt.Errorf("outer ring has fewer than three vertices")
	}
	n := unit(normal)
	if length(n) == 0 {
		return face{}, fmt.Errorf("zero face normal")
	}
	f := face{factor: n, useXY: math.Abs(n.z) > verticalNormalZ}
	if !f.useXY {
		axis := vec{0, 0, 1}
		if math.Abs(dot(n, axis)) > 0.9 {
			axis = vec{0, 1, 0}
		}
		f.u = unit(cross(axis, n))
		f.v = unit(cross(n, f.u))
	}
	f.planeD = dot(n, outer[0])
	f.bounds = bounds3{outer[0].x, outer[0].y, outer[0].z, outer[0].x, outer[0].y, outer[0].z}
	for _, p := range outer {
		f.bounds.minX = math.Min(f.bounds.minX, p.x)
		f.bounds.minY = math.Min(f.bounds.minY, p.y)
		f.bounds.minZ = math.Min(f.bounds.minZ, p.z)
		f.bounds.maxX = math.Max(f.bounds.maxX, p.x)
		f.bounds.maxY = math.Max(f.bounds.maxY, p.y)
		f.bounds.maxZ = math.Max(f.bounds.maxZ, p.z)
	}
	var err error
	f.outer, err = buildRing(outer, f)
	if err != nil {
		return face{}, fmt.Errorf("invalid outer ring: %w", err)
	}
	indices := make([]int, 0, len(holes))
	for index := range holes {
		indices = append(indices, index)
	}
	sort.Ints(indices)
	for _, index := range indices {
		h, e := buildRing(holes[index], f)
		if e != nil {
			return face{}, fmt.Errorf("invalid hole %d: %w", index, e)
		}
		f.holes = append(f.holes, h)
	}
	return f, nil
}

func loadgeo(path string) ([]face, error) {
	f, e := os.Open(path)
	if e != nil {
		return nil, e
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	s.Buffer(make([]byte, 4096), 4*1024*1024)
	faces := make([]face, 0)
	var normal vec
	var outer []vec
	holes := map[int][]vec{}
	block := 0
	finish := func() error {
		if len(outer) == 0 && len(holes) == 0 {
			return nil
		}
		block++
		if len(outer) == 0 {
			return fmt.Errorf("geometry block %d has holes but no outer ring", block)
		}
		face, err := prepareFace(outer, holes, normal)
		if err != nil {
			return fmt.Errorf("geometry block %d: %w", block, err)
		}
		faces = append(faces, face)
		return nil
	}
	for s.Scan() {
		line := strings.TrimSpace(strings.TrimSuffix(s.Text(), "\r"))
		if line == "" || strings.HasPrefix(line, "!") {
			continue
		}
		if line == ";" {
			if e = finish(); e != nil {
				return nil, e
			}
			normal = vec{}
			outer = nil
			holes = map[int][]vec{}
			continue
		}
		p := strings.Split(line, ",")
		switch strings.TrimSpace(p[0]) {
		case "f":
			continue
		case "fn":
			if len(p) < 4 {
				return nil, fmt.Errorf("geometry block %d: invalid fn", block+1)
			}
			normal, e = parseVec(p)
			if e != nil {
				return nil, fmt.Errorf("geometry block %d: invalid fn: %w", block+1, e)
			}
		case "fv":
			var point vec
			point, e = parseVec(p)
			if e != nil {
				return nil, fmt.Errorf("geometry block %d: invalid fv: %w", block+1, e)
			}
			outer = append(outer, point)
		case "fh":
			if len(p) < 5 {
				return nil, fmt.Errorf("geometry block %d: invalid fh", block+1)
			}
			index, err := strconv.Atoi(strings.TrimSpace(p[1]))
			if err != nil || index < 0 {
				return nil, fmt.Errorf("geometry block %d: invalid hole index", block+1)
			}
			point, err := parseVec([]string{"fh", p[2], p[3], p[4]})
			if err != nil {
				return nil, fmt.Errorf("geometry block %d: invalid fh: %w", block+1, err)
			}
			holes[index] = append(holes[index], point)
		default:
			return nil, fmt.Errorf("geometry block %d: unknown record %q", block+1, p[0])
		}
	}
	if e = s.Err(); e != nil {
		return nil, e
	}
	if e = finish(); e != nil {
		return nil, e
	}
	if len(faces) == 0 {
		return nil, fmt.Errorf("geometry contains no faces")
	}
	return faces, nil
}

func insideRing(p vec2, r ring) (inside, boundary bool) {
	if p.x < r.bounds.minX-boundaryTolerance || p.x > r.bounds.maxX+boundaryTolerance || p.y < r.bounds.minY-boundaryTolerance || p.y > r.bounds.maxY+boundaryTolerance {
		return false, false
	}
	inside = false
	n := len(r.points)
	for i := 0; i < n; i++ {
		a, b := r.points[i], r.points[(i+1)%n]
		dx, dy := b.x-a.x, b.y-a.y
		crossValue := (p.x-a.x)*dy - (p.y-a.y)*dx
		if math.Abs(crossValue) <= r.boundaryCross[i] && p.x >= math.Min(a.x, b.x)-boundaryTolerance && p.x <= math.Max(a.x, b.x)+boundaryTolerance && p.y >= math.Min(a.y, b.y)-boundaryTolerance && p.y <= math.Max(a.y, b.y)+boundaryTolerance {
			return true, true
		}
		if (a.y > p.y) != (b.y > p.y) && p.x < (b.x-a.x)*(p.y-a.y)/(b.y-a.y)+a.x {
			inside = !inside
		}
	}
	return inside, false
}

func pointInFace(point vec, mesh face) bool {
	p := projectPoint(point, mesh)
	in, boundary := insideRing(p, mesh.outer)
	if !in || boundary {
		return in
	}
	for _, hole := range mesh.holes {
		inHole, onBoundary := insideRing(p, hole)
		if onBoundary {
			return true
		}
		if inHole {
			return false
		}
	}
	return true
}

func intersectionDistance(rayline ray, mesh face) (float64, bool) {
	denominator := dot(mesh.factor, rayline.direction)
	if math.Abs(denominator) <= parallelTolerance {
		return 0, false
	}
	t := (mesh.planeD - dot(mesh.factor, rayline.origin)) / denominator
	return t, isFinite(t) && t > minimumRayDistance
}

func reflectedDirection(direction, normal vec) vec {
	if dot(normal, direction) < 0 {
		normal = negative(normal)
	}
	return unit(sub(direction, multi(2*dot(direction, normal), normal)))
}
func intersection(rayline ray, mesh face) ray {
	t, ok := intersectionDistance(rayline, mesh)
	if !ok {
		return ray{vec{-1, -1, -1}, vec{-1, -1, -1}}
	}
	return ray{add(rayline.origin, multi(t, rayline.direction)), reflectedDirection(rayline.direction, mesh.factor)}
}

func withinBounds(p vec, b bounds3) bool {
	return p.x >= b.minX-boundaryTolerance && p.x <= b.maxX+boundaryTolerance && p.y >= b.minY-boundaryTolerance && p.y <= b.maxY+boundaryTolerance && p.z >= b.minZ-boundaryTolerance && p.z <= b.maxZ+boundaryTolerance
}
func rayFaceTestWithCandidates(rayline ray, meshes []face, candidates []candidate) (ray, []candidate) {
	candidates = candidates[:0]
	for index := range meshes {
		f := meshes[index]
		t, ok := intersectionDistance(rayline, f)
		if !ok {
			continue
		}
		p := add(rayline.origin, multi(t, rayline.direction))
		if !withinBounds(p, f.bounds) {
			continue
		}
		candidates = append(candidates, candidate{t, index})
	}
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].distance == candidates[j].distance {
			return candidates[i].faceIndex < candidates[j].faceIndex
		}
		return candidates[i].distance < candidates[j].distance
	})
	for _, c := range candidates {
		p := add(rayline.origin, multi(c.distance, rayline.direction))
		f := meshes[c.faceIndex]
		if pointInFace(p, f) {
			return ray{p, reflectedDirection(rayline.direction, f.factor)}, candidates
		}
	}
	return ray{vec{-1, -1, -1}, vec{-1, -1, -1}}, candidates
}
func rayFaceTest(rayline ray, meshes []face) ray {
	hit, _ := rayFaceTestWithCandidates(rayline, meshes, make([]candidate, 0, len(meshes)))
	return hit
}

func formatRayOutput(r ray) string {
	return string(appendRayOutput(make([]byte, 0, 128), r))
}
func appendRayOutput(buffer []byte, r ray) []byte {
	buffer = buffer[:0]
	for i, v := range [6]float64{r.origin.x, r.origin.y, r.origin.z, r.direction.x, r.direction.y, r.direction.z} {
		if i > 0 {
			buffer = append(buffer, ',')
		}
		buffer = strconv.AppendFloat(buffer, v, 'f', 2, 64)
	}
	return buffer
}
func workerCount(n int) int {
	if n < 1 {
		return 1
	}
	c := runtime.GOMAXPROCS(0)
	if c < 1 {
		return 1
	}
	if c > n {
		return n
	}
	return c
}

type simulationInfo struct{ inputFile, outputFile, geometryFile string }

func help() {
	fmt.Println("Moosas rayTest.\nUsage: MoosasRad [-h,-g geometry.geo,-o output.o] input.i")
}
func main() {
	info := simulationInfo{outputFile: "MoosasRad.o", inputFile: "MoosasRad.i", geometryFile: "MoosasRadGeometry.geo"}
	for i := 1; i < len(os.Args); i++ {
		switch os.Args[i] {
		case "-h", "-help":
			help()
			return
		case "-g", "-geo":
			if i+1 >= len(os.Args) {
				fmt.Fprintln(os.Stderr, "missing geometry path")
				os.Exit(2)
			}
			i++
			info.geometryFile = os.Args[i]
		case "-o", "-output":
			if i+1 >= len(os.Args) {
				fmt.Fprintln(os.Stderr, "missing output path")
				os.Exit(2)
			}
			i++
			info.outputFile = os.Args[i]
		default:
			info.inputFile = os.Args[i]
		}
	}
	var e error
	info.inputFile, e = filepath.Abs(info.inputFile)
	if e != nil {
		fatal(e)
	}
	info.outputFile, e = filepath.Abs(info.outputFile)
	if e != nil {
		fatal(e)
	}
	info.geometryFile, e = filepath.Abs(info.geometryFile)
	if e != nil {
		fatal(e)
	}
	if e = simulation(info); e != nil {
		fatal(e)
	}
}
func fatal(e error) { fmt.Fprintln(os.Stderr, e); os.Exit(1) }
func simulation(info simulationInfo) error {
	rays, e := loadRay(info.inputFile)
	if e != nil {
		return fmt.Errorf("load rays: %w", e)
	}
	meshes, e := loadgeo(info.geometryFile)
	if e != nil {
		return fmt.Errorf("load geometry: %w", e)
	}
	index := buildBVH(meshes)
	results := make([]string, len(rays))
	jobs := make(chan int, workerCount(len(rays))*2)
	var wg sync.WaitGroup
	for w := 0; w < workerCount(len(rays)); w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			stack := make([]bvhVisit, 0, 64)
			buffer := make([]byte, 0, 128)
			for i := range jobs {
				hit, _, _, nextStack := index.trace(rays[i], stack, nil)
				stack = nextStack
				buffer = appendRayOutput(buffer, hit)
				results[i] = string(buffer)
			}
		}()
	}
	for i := range rays {
		jobs <- i
	}
	close(jobs)
	wg.Wait()
	file, e := os.Create(info.outputFile)
	if e != nil {
		return e
	}
	writer := bufio.NewWriter(file)
	for i, line := range results {
		if i > 0 {
			if _, e = writer.WriteString("\n"); e != nil {
				file.Close()
				return e
			}
		}
		if _, e = writer.WriteString(line); e != nil {
			file.Close()
			return e
		}
	}
	if e = writer.Flush(); e != nil {
		file.Close()
		return e
	}
	return file.Close()
}
