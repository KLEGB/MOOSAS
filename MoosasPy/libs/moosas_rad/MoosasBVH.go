package main

import "math"

const bvhBins = 16
const bvhLeafSize = 8
const bvhMaxDepth = 48

type bvhNode struct {
	bounds                    bounds3
	left, right, start, count int
}
type bvh struct {
	faces   []face
	indices []int
	nodes   []bvhNode
}
type bvhVisit struct {
	node  int
	entry float64
}

// Optional worker-local diagnostics; production passes nil (no atomic counters).
type traceStats struct{ Nodes, Faces, Polygons uint64 }
type bvhBucket struct {
	bounds bounds3
	count  int
}

func mergeBounds(a, b bounds3) bounds3 {
	return bounds3{math.Min(a.minX, b.minX), math.Min(a.minY, b.minY), math.Min(a.minZ, b.minZ), math.Max(a.maxX, b.maxX), math.Max(a.maxY, b.maxY), math.Max(a.maxZ, b.maxZ)}
}
func paddedBounds(b bounds3) bounds3 {
	return bounds3{
		math.Nextafter(b.minX-boundaryTolerance, math.Inf(-1)), math.Nextafter(b.minY-boundaryTolerance, math.Inf(-1)), math.Nextafter(b.minZ-boundaryTolerance, math.Inf(-1)),
		math.Nextafter(b.maxX+boundaryTolerance, math.Inf(1)), math.Nextafter(b.maxY+boundaryTolerance, math.Inf(1)), math.Nextafter(b.maxZ+boundaryTolerance, math.Inf(1)),
	}
}
func boundsArea(b bounds3) float64 {
	x, y, z := b.maxX-b.minX, b.maxY-b.minY, b.maxZ-b.minZ
	return 2 * (x*y + x*z + y*z)
}
func center(b bounds3, axis int) float64 {
	switch axis {
	case 0:
		return b.minX + (b.maxX-b.minX)*.5
	case 1:
		return b.minY + (b.maxY-b.minY)*.5
	default:
		return b.minZ + (b.maxZ-b.minZ)*.5
	}
}
func (b *bvhBucket) include(box bounds3) {
	if b.count == 0 {
		b.bounds = box
	} else {
		b.bounds = mergeBounds(b.bounds, box)
	}
	b.count++
}
func binIndex(value, minimum, scale float64) int {
	i := int((value - minimum) * scale)
	if i < 0 {
		return 0
	}
	if i >= bvhBins {
		return bvhBins - 1
	}
	return i
}
func buildBVH(faces []face) *bvh {
	tree := &bvh{faces: faces, indices: make([]int, len(faces)), nodes: make([]bvhNode, 0, 2*len(faces))}
	if len(faces) == 0 {
		return tree
	}
	boxes := make([]bounds3, len(faces))
	for i := range faces {
		tree.indices[i] = i
		boxes[i] = paddedBounds(faces[i].bounds)
	}
	scratch := make([]int, len(faces))
	var split func(int, int, int) int
	split = func(start, end, depth int) int {
		box := boxes[tree.indices[start]]
		for _, id := range tree.indices[start+1 : end] {
			box = mergeBounds(box, boxes[id])
		}
		nodeID := len(tree.nodes)
		tree.nodes = append(tree.nodes, bvhNode{bounds: box, start: start, count: end - start, left: -1, right: -1})
		if end-start <= bvhLeafSize || depth >= bvhMaxDepth {
			return nodeID
		}
		bestCost := float64(end-start) * boundsArea(box)
		bestAxis, bestBin := -1, -1
		bestMin, bestScale := 0.0, 0.0
		for axis := 0; axis < 3; axis++ {
			minimum, maximum := math.Inf(1), math.Inf(-1)
			for _, id := range tree.indices[start:end] {
				c := center(boxes[id], axis)
				minimum = math.Min(minimum, c)
				maximum = math.Max(maximum, c)
			}
			if maximum <= minimum {
				continue
			}
			scale := float64(bvhBins) / (maximum - minimum)
			if !isFinite(scale) {
				continue
			}
			var buckets [bvhBins]bvhBucket
			for _, id := range tree.indices[start:end] {
				buckets[binIndex(center(boxes[id], axis), minimum, scale)].include(boxes[id])
			}
			var leftArea, rightArea [bvhBins]float64
			var leftCount, rightCount [bvhBins]int
			var merged bounds3
			n := 0
			for i := 0; i < bvhBins; i++ {
				if buckets[i].count > 0 {
					if n == 0 {
						merged = buckets[i].bounds
					} else {
						merged = mergeBounds(merged, buckets[i].bounds)
					}
					n += buckets[i].count
				}
				leftCount[i] = n
				if n > 0 {
					leftArea[i] = boundsArea(merged)
				}
			}
			n = 0
			for i := bvhBins - 1; i >= 0; i-- {
				if buckets[i].count > 0 {
					if n == 0 {
						merged = buckets[i].bounds
					} else {
						merged = mergeBounds(merged, buckets[i].bounds)
					}
					n += buckets[i].count
				}
				rightCount[i] = n
				if n > 0 {
					rightArea[i] = boundsArea(merged)
				}
			}
			for i := 0; i < bvhBins-1; i++ {
				if leftCount[i] == 0 || rightCount[i+1] == 0 {
					continue
				}
				cost := boundsArea(box) + float64(leftCount[i])*leftArea[i] + float64(rightCount[i+1])*rightArea[i+1]
				if cost < bestCost {
					bestCost = cost
					bestAxis = axis
					bestBin = i
					bestMin = minimum
					bestScale = scale
				}
			}
		}
		if bestAxis < 0 {
			return nodeID
		}
		// Stable partition retains original face order for equal centroids.
		middle := start
		for _, id := range tree.indices[start:end] {
			if binIndex(center(boxes[id], bestAxis), bestMin, bestScale) <= bestBin {
				scratch[middle] = id
				middle++
			}
		}
		cursor := middle
		for _, id := range tree.indices[start:end] {
			if binIndex(center(boxes[id], bestAxis), bestMin, bestScale) > bestBin {
				scratch[cursor] = id
				cursor++
			}
		}
		copy(tree.indices[start:end], scratch[start:end])
		left, right := split(start, middle, depth+1), split(middle, end, depth+1)
		tree.nodes[nodeID].left = left
		tree.nodes[nodeID].right = right
		tree.nodes[nodeID].count = 0
		return nodeID
	}
	split(0, len(faces), 0)
	return tree
}

type slabRay struct{ origin, direction, inverse [3]float64 }

func prepareSlab(r ray) slabRay {
	s := slabRay{origin: [3]float64{r.origin.x, r.origin.y, r.origin.z}, direction: [3]float64{r.direction.x, r.direction.y, r.direction.z}}
	for i, d := range s.direction {
		if d != 0 {
			s.inverse[i] = 1 / d
		}
	}
	return s
}
func (r slabRay) interval(b bounds3, limit float64) (float64, bool) {
	lo, hi := minimumRayDistance, math.Nextafter(limit, math.Inf(1))
	minimum := [3]float64{b.minX, b.minY, b.minZ}
	maximum := [3]float64{b.maxX, b.maxY, b.maxZ}
	for axis, d := range r.direction {
		o := r.origin[axis]
		if d == 0 {
			if o < minimum[axis] || o > maximum[axis] {
				return 0, false
			}
			continue
		}
		a, c := (minimum[axis]-o)*r.inverse[axis], (maximum[axis]-o)*r.inverse[axis]
		if math.IsInf(r.inverse[axis], 0) {
			a = (minimum[axis] - o) / d
			c = (maximum[axis] - o) / d
		}
		if a > c {
			a, c = c, a
		}
		a = math.Nextafter(a, math.Inf(-1))
		c = math.Nextafter(c, math.Inf(1))
		lo = math.Max(lo, a)
		hi = math.Min(hi, c)
		if lo > hi {
			return 0, false
		}
	}
	return lo, true
}

func (tree *bvh) trace(r ray, stack []bvhVisit, stats *traceStats) (ray, int, float64, []bvhVisit) {
	stack = stack[:0]
	miss := ray{vec{-1, -1, -1}, vec{-1, -1, -1}}
	if len(tree.nodes) == 0 {
		return miss, -1, math.Inf(1), stack
	}
	slab := prepareSlab(r)
	entry, ok := slab.interval(tree.nodes[0].bounds, math.Inf(1))
	if !ok {
		return miss, -1, math.Inf(1), stack
	}
	stack = append(stack, bvhVisit{0, entry})
	bestT, bestID := math.Inf(1), -1
	for len(stack) > 0 {
		visit := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if visit.entry > math.Nextafter(bestT, math.Inf(1)) {
			continue
		}
		node := tree.nodes[visit.node]
		if stats != nil {
			stats.Nodes++
		}
		if node.count > 0 {
			for _, id := range tree.indices[node.start : node.start+node.count] {
				if stats != nil {
					stats.Faces++
				}
				f := tree.faces[id]
				t, valid := intersectionDistance(r, f)
				if !valid || t > bestT || (t == bestT && id >= bestID) {
					continue
				}
				p := add(r.origin, multi(t, r.direction))
				if !withinBounds(p, f.bounds) {
					continue
				}
				if stats != nil {
					stats.Polygons++
				}
				if pointInFace(p, f) {
					bestT, bestID = t, id
				}
			}
			continue
		}
		left, lOK := slab.interval(tree.nodes[node.left].bounds, bestT)
		right, rOK := slab.interval(tree.nodes[node.right].bounds, bestT)
		if lOK && rOK {
			if left <= right {
				stack = append(stack, bvhVisit{node.right, right}, bvhVisit{node.left, left})
			} else {
				stack = append(stack, bvhVisit{node.left, left}, bvhVisit{node.right, right})
			}
		} else if lOK {
			stack = append(stack, bvhVisit{node.left, left})
		} else if rOK {
			stack = append(stack, bvhVisit{node.right, right})
		}
	}
	if bestID < 0 {
		return miss, -1, bestT, stack
	}
	return ray{add(r.origin, multi(bestT, r.direction)), reflectedDirection(r.direction, tree.faces[bestID].factor)}, bestID, bestT, stack
}
