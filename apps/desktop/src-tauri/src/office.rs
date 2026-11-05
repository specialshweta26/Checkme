//! Text from PowerPoint, Word and Excel files on this computer, so the agent can work on them without those apps.
//! These formats are zip files of XML, so reading the text doesn't need anything else installed.

use std::io::{Cursor, Read};
use std::path::Path;

const MAX_PART_BYTES: u64 = 20 * 1024 * 1024;

fn extension(file: &Path) -> String {
    file.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase()
}

pub fn is_office(file: &Path) -> bool {
    matches!(extension(file).as_str(), "pptx" | "docx" | "xlsx")
}

fn unescape(text: &str) -> String {
    text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"").replace("&apos;", "'").replace("&amp;", "&")
}

/// The text inside every `<tag ...>...</tag>`, in order. `<tag/>` and longer names like `<a:tab>` don't count.
fn texts_of(xml: &str, tag: &str) -> Vec<String> {
    let open = format!("<{tag}");
    let close = format!("</{tag}>");
    let mut out = Vec::new();
    let mut rest = xml;
    while let Some(start) = rest.find(&open) {
        let after = &rest[start + open.len()..];
        let Some(end_of_open) = after.find('>') else { break };
        let attrs = &after[..end_of_open];
        let body = &after[end_of_open + 1..];
        let whole_name = attrs.is_empty() || attrs.starts_with(char::is_whitespace);
        if !whole_name || attrs.ends_with('/') {
            rest = after;
            continue;
        }
        let Some(end) = body.find(&close) else { break };
        out.push(unescape(&body[..end]));
        rest = &body[end + close.len()..];
    }
    out
}

fn numbered_parts(names: &[String], prefix: &str) -> Vec<(u32, String)> {
    let mut parts: Vec<(u32, String)> = names
        .iter()
        .filter_map(|name| {
            let number = name.strip_prefix(prefix)?.strip_suffix(".xml")?.parse().ok()?;
            Some((number, name.clone()))
        })
        .collect();
    parts.sort();
    parts
}

pub fn text_of(file: &Path, bytes: &[u8]) -> Result<String, String> {
    let unreadable = |err: String| format!("{} couldn't be read: {err}", file.display());
    let mut archive = zip::ZipArchive::new(Cursor::new(bytes)).map_err(|e| unreadable(e.to_string()))?;
    let names: Vec<String> = archive.file_names().map(str::to_string).collect();
    let mut part = |name: &str| -> Result<String, String> {
        let entry = archive.by_name(name).map_err(|e| unreadable(e.to_string()))?;
        let mut xml = String::new();
        entry.take(MAX_PART_BYTES).read_to_string(&mut xml).map_err(|e| unreadable(e.to_string()))?;
        Ok(xml)
    };
    let text = match extension(file).as_str() {
        "pptx" => {
            let mut slides = Vec::new();
            for (number, name) in numbered_parts(&names, "ppt/slides/slide") {
                slides.push(format!("Slide {number}: {}", texts_of(&part(&name)?, "a:t").join(" ")));
            }
            slides.join("\n")
        }
        "docx" => {
            let xml = part("word/document.xml")?;
            texts_of(&xml, "w:p").iter().map(|p| texts_of(p, "w:t").concat()).filter(|p| !p.is_empty()).collect::<Vec<_>>().join("\n")
        }
        _ => {
            // Cell text lives in one shared list; numbers are in the sheets, which the agent can ask for as CSV.
            let shared = part("xl/sharedStrings.xml").unwrap_or_default();
            texts_of(&shared, "t").join("\n")
        }
    };
    if text.trim().is_empty() {
        return Err(format!("{} has no text to read, so its content may be images. Ask the user to attach it to the chat.", file.display()));
    }
    Ok(text)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use zip::write::SimpleFileOptions;

    fn archive(parts: &[(&str, &str)]) -> Vec<u8> {
        let mut out = zip::ZipWriter::new(Cursor::new(Vec::new()));
        for (name, body) in parts {
            out.start_file(*name, SimpleFileOptions::default()).unwrap();
            out.write_all(body.as_bytes()).unwrap();
        }
        out.finish().unwrap().into_inner()
    }

    #[test]
    fn pptx_slides_come_back_in_order() {
        let bytes = archive(&[
            ("ppt/slides/slide10.xml", "<a:t>Tenth</a:t>"),
            ("ppt/slides/slide2.xml", "<a:t>Soil &amp; water</a:t><a:tab/><a:t lang=\"en\">care</a:t>"),
            ("ppt/slides/slide1.xml", "<a:t>Organic farming</a:t>"),
            ("ppt/slides/_rels/slide1.xml.rels", "<a:t>ignored</a:t>"),
        ]);
        let text = text_of(Path::new("deck.PPTX"), &bytes).unwrap();
        assert_eq!(text, "Slide 1: Organic farming\nSlide 2: Soil & water care\nSlide 10: Tenth");
    }

    #[test]
    fn docx_keeps_paragraphs() {
        let bytes = archive(&[("word/document.xml", "<w:p><w:r><w:t>Hello </w:t><w:t>world</w:t></w:r></w:p><w:p/><w:pPr/><w:p x=\"1\"><w:t>Next</w:t></w:p>")]);
        assert_eq!(text_of(Path::new("a.docx"), &bytes).unwrap(), "Hello world\nNext");
    }

    #[test]
    fn xlsx_reads_shared_text() {
        let bytes = archive(&[("xl/sharedStrings.xml", "<si><t>Region</t></si><si><t xml:space=\"preserve\">West</t></si>")]);
        assert_eq!(text_of(Path::new("a.xlsx"), &bytes).unwrap(), "Region\nWest");
    }

    #[test]
    fn a_broken_or_empty_file_says_so() {
        assert!(text_of(Path::new("a.pptx"), b"not a zip").unwrap_err().contains("couldn't be read"));
        let empty = archive(&[("ppt/slides/slide1.xml", "<p:pic/>")]);
        assert!(text_of(Path::new("a.pptx"), &empty).is_ok()); // a slide with no words still lists the slide
        let no_slides = archive(&[("other.xml", "<x/>")]);
        assert!(text_of(Path::new("a.pptx"), &no_slides).unwrap_err().contains("no text"));
    }

    #[test]
    fn only_these_formats_count() {
        assert!(is_office(Path::new("A.PPTX")) && is_office(Path::new("a.docx")) && is_office(Path::new("a.xlsx")));
        assert!(!is_office(Path::new("a.ppt")) && !is_office(Path::new("a.pdf")) && !is_office(Path::new("pptx")));
    }
}
