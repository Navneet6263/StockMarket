from youtube_transcript_api import YouTubeTranscriptApi
import json
import sys

def get_transcript(video_id):
    try:
        # Fetching the transcript
        transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=['hi', 'en'])
        
        # Combining the text
        full_text = " ".join([entry['text'] for entry in transcript])
        
        with open("transcript.txt", "w", encoding="utf-8") as f:
            f.write(full_text)
            
        print("Transcript saved to transcript.txt")
    except Exception as e:
        print(f"Error fetching transcript: {e}")

if __name__ == "__main__":
    get_transcript("riCzcmAuewI")
