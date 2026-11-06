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

const MAX_SHEET_ROWS: usize = 500;

/// The value of `name="..."` in a tag's attributes.
fn attr(attrs: &str, name: &str) -> String {
    let key = format!(" {name}=\"");
    attrs.find(&key).map_or(String::new(), |at| {
        let rest = &attrs[at + key.len()..];
        rest[..rest.find('"').unwrap_or(rest.len())].to_string()
    })
}

/// Each `<c ...>...</c>` in a row: its type attribute and body. Empty `<c .../>` cells keep their place.
fn cells_of(row: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    let mut rest = row;
    while let Some(start) = rest.find("<c") {
        let after = &rest[start + 2..];
        if !(after.starts_with(' ') || after.starts_with('>') || after.starts_with('/')) {
            rest = after;
            continue;
        }
        let Some(end_of_open) = after.find('>') else { break };
        let attrs = &after[..end_of_open];
        if attrs.ends_with('/') {
            out.push((attr(attrs, "t"), String::new()));
            rest = &after[end_of_open + 1..];
            continue;
        }
        let body = &after[end_of_open + 1..];
        let Some(end) = body.find("</c>") else { break };
        out.push((attr(attrs, "t"), body[..end].to_string()));
        rest = &body[end + 4..];
    }
    out
}

fn cell_text(kind: &str, body: &str, shared: &[String]) -> String {
    let value = texts_of(body, "v").into_iter().next().unwrap_or_default();
    match kind {
        "s" => value.parse::<usize>().ok().and_then(|i| shared.get(i).cloned()).unwrap_or_default(),
        "inlineStr" => texts_of(body, "t").concat(),
        _ => value,
    }
}

fn sheet_names(workbook: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut rest = workbook;
    while let Some(start) = rest.find("<sheet ") {
        let after = &rest[start..];
        let end = after.find('>').unwrap_or(after.len());
        out.push(unescape(&attr(&after[..end], "name")));
        rest = &after[end..];
    }
    out
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
            let shared: Vec<String> = texts_of(&part("xl/sharedStrings.xml").unwrap_or_default(), "si")
                .iter()
                .map(|item| texts_of(item, "t").concat())
                .collect();
            let titles = sheet_names(&part("xl/workbook.xml").unwrap_or_default());
            let mut out = Vec::new();
            for (index, (number, name)) in numbered_parts(&names, "xl/worksheets/sheet").into_iter().enumerate() {
                let title = titles.get(index).cloned().unwrap_or_else(|| format!("Sheet {number}"));
                out.push(format!("Sheet: {title}"));
                for (count, row) in texts_of(&part(&name)?, "row").iter().enumerate() {
                    if count >= MAX_SHEET_ROWS {
                        out.push("(more rows not shown)".into());
                        break;
                    }
                    let cells: Vec<String> = cells_of(row).iter().map(|(kind, body)| cell_text(kind, body, &shared)).collect();
                    if cells.iter().any(|c| !c.is_empty()) {
                        out.push(cells.join("\t"));
                    }
                }
            }
            out.join("\n")
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
    fn xlsx_reads_every_cell_by_row() {
        let bytes = archive(&[
            ("xl/workbook.xml", "<sheets><sheet name=\"Shoes &amp; prices\" sheetId=\"1\"/></sheets>"),
            ("xl/sharedStrings.xml", "<si><t>Name</t></si><si><t xml:space=\"preserve\">Price</t></si><si><r><t>Run</t></r><r><t>ner X</t></r></si>"),
            ("xl/worksheets/sheet1.xml", concat!(
                "<sheetData><row r=\"1\"><c r=\"A1\" t=\"s\"><v>0</v></c><c r=\"B1\" t=\"s\"><v>1</v></c></row>",
                "<row r=\"2\"><c r=\"A2\" t=\"s\"><v>2</v></c><c r=\"B2\"><v>7499</v></c><c r=\"C2\" t=\"inlineStr\"><is><t>new</t></is></c></row>",
                "<row r=\"3\"><c r=\"A3\"/></row></sheetData>",
            )),
        ]);
        assert_eq!(text_of(Path::new("a.xlsx"), &bytes).unwrap(), "Sheet: Shoes & prices\nName\tPrice\nRunner X\t7499\tnew");
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
