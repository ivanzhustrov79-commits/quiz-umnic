import json

print("Loading databases...")
with open('quiz_database_440.json', 'r', encoding='utf-8') as f:
    db1 = json.load(f)

with open('quiz_database_235.json', 'r', encoding='utf-8') as f:
    db2 = json.load(f)

# Combine the questions arrays
questions = db1.get('questions', []) + db2.get('questions', [])
print(f"Combined raw total: {len(questions)} questions.")

# Remove exact duplicates based on the Russian question text
seen_texts = set()
unique_questions = []
for q in questions:
    text = q.get('question_ru', '').strip().lower()
    if text and text not in seen_texts:
        seen_texts.add(text)
        unique_questions.append(q)

# Re-index IDs sequentially to prevent Gate 1 Duplicate ID failures
print("Re-indexing IDs to prevent duplicates...")
for i, q in enumerate(unique_questions, start=1):
    q['id'] = f"q{i:04d}"

# Create final clean structure
final_db = {
    "_meta": {
        "total": len(unique_questions),
        "sources": ["quiz_database_440.json", "quiz_database_235.json"],
        "note": "Merged from existing databases and re-indexed."
    },
    "questions": unique_questions
}

# Save the final file
output_file = 'quiz_database_final.json'
with open(output_file, 'w', encoding='utf-8') as f:
    json.dump(final_db, f, ensure_ascii=False, indent=2)

print(f"\n✅ SUCCESS! Saved {len(unique_questions)} unique questions to {output_file}")
print("You can now use this file for your app!")