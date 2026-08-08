// vision_ocr — on-device OCR using Apple's Vision framework (the Mac-native
// on-device text reader). Reads an image, prints one
// JSON array of recognized text boxes with TOP-LEFT-origin normalized coords.
//
//   swiftc -O tools/vision_ocr.swift -o bin/vision_ocr
//   bin/vision_ocr /path/to/screen.png
import Foundation
import Vision
import AppKit

struct Box: Codable { let text: String; let conf: Float
                      let x: Double; let y: Double; let w: Double; let h: Double }

guard CommandLine.arguments.count >= 2,
      let img = NSImage(contentsOfFile: CommandLine.arguments[1]),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("usage: vision_ocr <image>\n".data(using: .utf8)!)
    exit(2)
}

let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.usesLanguageCorrection = false          // handles/usernames aren't dictionary words

let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do { try handler.perform([req]) } catch { exit(3) }

var out: [Box] = []
for obs in (req.results ?? []) {
    guard let top = obs.topCandidates(1).first else { continue }
    let b = obs.boundingBox                 // normalized, origin BOTTOM-left
    out.append(Box(text: top.string, conf: top.confidence,
                   x: Double(b.minX), y: Double(1.0 - b.maxY),   // flip to TOP-left
                   w: Double(b.width), h: Double(b.height)))
}
let data = try! JSONEncoder().encode(out)
print(String(data: data, encoding: .utf8)!)
