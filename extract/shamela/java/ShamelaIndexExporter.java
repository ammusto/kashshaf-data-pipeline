import java.io.*;
import java.util.*;
import java.nio.charset.StandardCharsets;
import org.apache.lucene.document.*;
import org.apache.lucene.index.*;
import org.apache.lucene.store.*;
import org.apache.lucene.util.*;

public class ShamelaIndexExporter {
    private static final String BOOK_OUTPUT_DIR = "book_data";
    private static final String TITLE_OUTPUT_DIR = "title_data";
    private static final String BOOK_INDEX_NAME = "page";
    private static final String TITLE_INDEX_NAME = "title";
    
    private static String BASE_INDEX_PATH;
    private static String OUTPUT_DIR;
    private static boolean TEST_MODE = false;

    public static void main(String[] args) {
        if (args.length < 2) {
            System.out.println("Usage: ShamelaIndexExporter <shamela_store_path> <output_directory> [--test-single-book]");
            System.out.println("Example: ShamelaIndexExporter /path/to/shamela4/database/store ./exported_indices");
            return;
        }
        
        BASE_INDEX_PATH = args[0];
        OUTPUT_DIR = args[1];
        
        if (args.length > 2 && args[2].equals("--test-single-book")) {
            TEST_MODE = true;
            System.out.println("TEST MODE: Will process only first book found");
        }
        
        System.out.println("Starting Shamela Index Export");
        System.out.println("Source: " + BASE_INDEX_PATH);
        System.out.println("Output: " + OUTPUT_DIR);
        System.out.println();

        try {
            File outputDirFile = new File(OUTPUT_DIR);
            if (!outputDirFile.exists()) {
                outputDirFile.mkdirs();
                System.out.println("Created output directory: " + OUTPUT_DIR);
            }

            File booksDir = new File(OUTPUT_DIR + File.separator + BOOK_OUTPUT_DIR);
            if (!booksDir.exists()) {
                booksDir.mkdirs();
                System.out.println("Created books directory: " + booksDir.getPath());
            }

            File titlesDir = new File(OUTPUT_DIR + File.separator + TITLE_OUTPUT_DIR);
            if (!titlesDir.exists()) {
                titlesDir.mkdirs();
                System.out.println("Created titles directory: " + titlesDir.getPath());
            }

            File baseDir = new File(BASE_INDEX_PATH);
            if (!baseDir.exists()) {
                System.out.println("ERROR: Base index path does not exist: " + BASE_INDEX_PATH);
                return;
            }
            
            File[] indexDirs = baseDir.listFiles(File::isDirectory);

            if (indexDirs == null || indexDirs.length == 0) {
                System.out.println("No subdirectories found in " + BASE_INDEX_PATH);
                return;
            }

            System.out.println("Found " + indexDirs.length + " index directories to process");
            System.out.println();

            for (File indexDir : indexDirs) {
                String indexName = indexDir.getName();
                System.out.println("Processing index: " + indexName);

                try {
                    Directory directory = FSDirectory.open(indexDir.toPath());

                    if (!DirectoryReader.indexExists(directory)) {
                        System.out.println("No valid Lucene index in: " + indexDir.getPath());
                        continue;
                    }

                    IndexReader reader = DirectoryReader.open(directory);

                    if (indexName.equalsIgnoreCase(BOOK_INDEX_NAME)) {
                        exportBookIndexToSeparateFiles(reader, indexName);
                    } else if (indexName.equalsIgnoreCase(TITLE_INDEX_NAME)) {
                        exportTitleIndexToSeparateFiles(reader, indexName);
                    } else {
                        exportStandardIndex(reader, indexName);
                    }

                    reader.close();
                    directory.close();

                } catch (IndexFormatTooNewException e) {
                    System.out.println("Index format too new in: " + indexDir.getPath() +
                            " - " + e.getMessage());
                } catch (Exception e) {
                    System.out.println("Error processing index: " + indexDir.getPath());
                    e.printStackTrace();
                }
            }

            System.out.println();
            System.out.println("All indices processed successfully!");

        } catch (Exception e) {
            System.out.println("Error in main execution:");
            e.printStackTrace();
        }
    }

    private static void exportStandardIndex(IndexReader reader, String indexName) throws IOException {
        System.out.println("Standard export for: " + indexName);
        Set<String> fieldNames = new HashSet<>();
        for (int i = 0; i < Math.min(1000, reader.maxDoc()); i++) {
            Document doc = reader.storedFields().document(i);
            for (IndexableField field : doc.getFields()) {
                fieldNames.add(field.name());
            }
        }
        List<String> fieldNameList = new ArrayList<>(fieldNames);
        String outputFile = OUTPUT_DIR + File.separator + indexName + ".csv";
        int totalDocs = reader.maxDoc();
        System.out.println("Exporting " + totalDocs + " documents...");
        try (OutputStreamWriter osw = new OutputStreamWriter(new FileOutputStream(outputFile), StandardCharsets.UTF_8);
                PrintWriter pw = new PrintWriter(osw)) {
            pw.println("DocID," + String.join(",", fieldNameList));
            for (int i = 0; i < totalDocs; i++) {
                Document doc = reader.storedFields().document(i);
                StringBuilder sb = new StringBuilder();
                sb.append(i);
                for (String fieldName : fieldNameList) {
                    sb.append(",");
                    String[] values = doc.getValues(fieldName);
                    if (values != null && values.length > 0) {
                        StringJoiner joiner = new StringJoiner(";");
                        for (String value : values) {
                            joiner.add(escapeCSV(value));
                        }
                        sb.append("\"").append(joiner.toString()).append("\"");
                    }
                }
                pw.println(sb.toString());
            }
        }
        System.out.println("Exported " + totalDocs + " documents to " + outputFile);
    }

    private static void exportBookIndexToSeparateFiles(IndexReader reader, String indexName) throws IOException {
        System.out.println("Exporting books to individual files");
        Set<String> fieldNames = new HashSet<>();
        for (int i = 0; i < Math.min(1000, reader.maxDoc()); i++) {
            Document doc = reader.storedFields().document(i);
            for (IndexableField field : doc.getFields()) {
                fieldNames.add(field.name());
            }
        }
        List<String> fieldNameList = new ArrayList<>(fieldNames);
        Set<String> bookIds = new HashSet<>();
        Terms terms = MultiTerms.getTerms(reader, "id");
        if (terms != null) {
            TermsEnum termsEnum = terms.iterator();
            BytesRef term;
            while ((term = termsEnum.next()) != null) {
                String idTerm = term.utf8ToString();
                if (idTerm.contains("-")) {
                    String bookId = idTerm.split("-")[0];
                    bookIds.add(bookId);
                }
            }
        }
        System.out.println("Found " + bookIds.size() + " unique book IDs");
        List<String> bookIdList = new ArrayList<>(bookIds);
        Collections.sort(bookIdList, Comparator.comparingInt(Integer::parseInt));
        String booksFolder = OUTPUT_DIR + File.separator + BOOK_OUTPUT_DIR;
        new File(booksFolder).mkdirs();
        for (String bookId : bookIdList) {
            System.out.println("Processing book: " + bookId);
            
            if (TEST_MODE && !bookId.equals(bookIdList.get(0))) {
                System.out.println("TEST MODE: Skipping book " + bookId + " (processing only first book)");
                continue;
            }
            String bookFile = booksFolder + File.separator + bookId + ".csv";
            try (OutputStreamWriter osw = new OutputStreamWriter(new FileOutputStream(bookFile), StandardCharsets.UTF_8);
                    PrintWriter pw = new PrintWriter(osw)) {
                pw.println("DocID,BookID,PageID," + String.join(",", fieldNameList));
                List<DocWithId> docsForBook = new ArrayList<>();

                List<LeafReaderContext> leaves = reader.leaves();

                TermsEnum termsEnum = terms.iterator();
                BytesRef prefix = new BytesRef(bookId + "-");

                termsEnum.seekCeil(prefix);

                BytesRef currentTerm = termsEnum.term();
                if (currentTerm != null && currentTerm.utf8ToString().startsWith(bookId + "-")) {
                    processTermForBook(reader, termsEnum, bookId, docsForBook, leaves);

                    while (true) {
                        BytesRef nextTerm = termsEnum.next();
                        if (nextTerm == null || !nextTerm.utf8ToString().startsWith(bookId + "-")) {
                            break;
                        }
                        processTermForBook(reader, termsEnum, bookId, docsForBook, leaves);
                    }
                }

                Collections.sort(docsForBook);

                for (DocWithId docWithId : docsForBook) {
                    Document doc = docWithId.getDoc();
                    int docId = docWithId.getDocId();
                    StringBuilder sb = new StringBuilder();
                    sb.append(docId).append(",");
                    sb.append(bookId).append(",");
                    sb.append(docWithId.getPageId());
                    for (String fieldName : fieldNameList) {
                        sb.append(",");
                        String[] values = doc.getValues(fieldName);
                        if (values != null && values.length > 0) {
                            StringJoiner joiner = new StringJoiner(";");
                            for (String value : values) {
                                joiner.add(escapeCSV(value));
                            }
                            sb.append("\"").append(joiner.toString()).append("\"");
                        }
                    }
                    pw.println(sb.toString());
                }
                System.out.println("Exported book " + bookId + " with " + docsForBook.size() + " pages");
            }
        }
        System.out.println("Finished exporting all books to individual files");
    }

    // Uses liveDocs to skip deleted documents
    private static void processTermForBook(IndexReader reader, TermsEnum termsEnum, String bookId,
            List<DocWithId> docsForBook, List<LeafReaderContext> leaves) throws IOException {
        PostingsEnum postings = termsEnum.postings(null);
        while (postings.nextDoc() != PostingsEnum.NO_MORE_DOCS) {
            int docId = postings.docID();

            int leafIdx = ReaderUtil.subIndex(docId, leaves);
            LeafReaderContext ctx = leaves.get(leafIdx);
            Bits liveDocs = ctx.reader().getLiveDocs();
            int localDocId = docId - ctx.docBase;

            if (liveDocs != null && !liveDocs.get(localDocId)) {
                continue;
            }

            Document doc = reader.storedFields().document(docId);
            String idValue = doc.get("id");
            if (idValue != null && idValue.startsWith(bookId + "-")) {
                String pageId = idValue.split("-")[1];
                docsForBook.add(new DocWithId(docId, doc, Integer.parseInt(pageId)));
            }
        }
    }

    private static void exportTitleIndexToSeparateFiles(IndexReader reader, String indexName) throws IOException {
        System.out.println("Exporting title index to individual book files");

        Set<String> bookIds = new HashSet<>();
        Terms terms = MultiTerms.getTerms(reader, "id");
        if (terms != null) {
            TermsEnum termsEnum = terms.iterator();
            BytesRef term;
            while ((term = termsEnum.next()) != null) {
                String idTerm = term.utf8ToString();
                if (idTerm.contains("-")) {
                    String bookId = idTerm.split("-")[0];
                    bookIds.add(bookId);
                }
            }
        }

        System.out.println("Found " + bookIds.size() + " unique book IDs in titles");

        String titlesFolder = OUTPUT_DIR + File.separator + TITLE_OUTPUT_DIR;
        new File(titlesFolder).mkdirs();

        List<String> bookIdList = new ArrayList<>(bookIds);
        Collections.sort(bookIdList, Comparator.comparingInt(Integer::parseInt));

        for (String bookId : bookIdList) {
            System.out.println("Processing titles for book: " + bookId);
            
            if (TEST_MODE && !bookId.equals(bookIdList.get(0))) {
                System.out.println("TEST MODE: Skipping titles for book " + bookId + " (processing only first book)");
                continue;
            }

            String titleFile = titlesFolder + File.separator + "title_" + bookId + ".csv";

            try (OutputStreamWriter osw = new OutputStreamWriter(new FileOutputStream(titleFile), StandardCharsets.UTF_8);
                    PrintWriter pw = new PrintWriter(osw)) {

                pw.println("DocID,id,body");

                List<TitleDoc> titlesForBook = new ArrayList<>();

                List<LeafReaderContext> leaves = reader.leaves();

                TermsEnum termsEnum = terms.iterator();
                BytesRef prefix = new BytesRef(bookId + "-");

                termsEnum.seekCeil(prefix);

                BytesRef currentTerm = termsEnum.term();
                if (currentTerm != null && currentTerm.utf8ToString().startsWith(bookId + "-")) {
                    processTermForTitle(reader, termsEnum, bookId, titlesForBook, leaves);

                    while (true) {
                        BytesRef nextTerm = termsEnum.next();
                        if (nextTerm == null || !nextTerm.utf8ToString().startsWith(bookId + "-")) {
                            break;
                        }
                        processTermForTitle(reader, termsEnum, bookId, titlesForBook, leaves);
                    }
                }

                Collections.sort(titlesForBook, Comparator.comparingInt(TitleDoc::getTitleId));

                for (TitleDoc titleDoc : titlesForBook) {
                    Document doc = titleDoc.getDoc();
                    int docId = titleDoc.getOrigDocId();
                    String idValue = doc.get("id");
                    String bodyValue = doc.get("body");

                    pw.println(docId + ",\"" + escapeCSV(idValue) + "\",\"" + escapeCSV(bodyValue) + "\"");
                }

                System.out.println("Exported " + titlesForBook.size() + " titles for book " + bookId);
            }
        }

        System.out.println("Finished exporting all titles to individual files");
    }

    // Uses liveDocs to skip deleted documents
    private static void processTermForTitle(IndexReader reader, TermsEnum termsEnum, String bookId,
            List<TitleDoc> titlesForBook, List<LeafReaderContext> leaves) throws IOException {
        PostingsEnum postings = termsEnum.postings(null);
        while (postings.nextDoc() != PostingsEnum.NO_MORE_DOCS) {
            int docId = postings.docID();

            int leafIdx = ReaderUtil.subIndex(docId, leaves);
            LeafReaderContext ctx = leaves.get(leafIdx);
            Bits liveDocs = ctx.reader().getLiveDocs();
            int localDocId = docId - ctx.docBase;

            if (liveDocs != null && !liveDocs.get(localDocId)) {
                continue;
            }

            Document doc = reader.storedFields().document(docId);
            String idValue = doc.get("id");

            if (idValue != null && idValue.startsWith(bookId + "-")) {
                String titleIdStr = idValue.split("-")[1];
                int titleId = Integer.parseInt(titleIdStr);
                titlesForBook.add(new TitleDoc(docId, doc, Integer.parseInt(bookId), titleId));
            }
        }
    }

    private static String escapeCSV(String value) {
        if (value == null)
            return "";
        return value.replace("\"", "\"\"");
    }

    static class DocWithId implements Comparable<DocWithId> {
        private final int docId;
        private final Document doc;
        private final int pageId;

        public DocWithId(int docId, Document doc, int pageId) {
            this.docId = docId;
            this.doc = doc;
            this.pageId = pageId;
        }

        public int getDocId() {
            return docId;
        }

        public Document getDoc() {
            return doc;
        }

        public int getPageId() {
            return pageId;
        }

        @Override
        public int compareTo(DocWithId other) {
            return Integer.compare(this.pageId, other.pageId);
        }
    }

    static class TitleDoc implements Comparable<TitleDoc> {
        private final int origDocId;
        private final Document doc;
        private final int bookId;
        private final int titleId;

        public TitleDoc(int origDocId, Document doc, int bookId, int titleId) {
            this.origDocId = origDocId;
            this.doc = doc;
            this.bookId = bookId;
            this.titleId = titleId;
        }

        public int getOrigDocId() {
            return origDocId;
        }

        public Document getDoc() {
            return doc;
        }

        public int getBookId() {
            return bookId;
        }

        public int getTitleId() {
            return titleId;
        }

        @Override
        public int compareTo(TitleDoc other) {
            int bookCompare = Integer.compare(this.bookId, other.bookId);
            if (bookCompare != 0) {
                return bookCompare;
            }

            return Integer.compare(this.titleId, other.titleId);
        }
    }
}