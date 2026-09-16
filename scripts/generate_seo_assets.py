import os
from PIL import Image, ImageDraw, ImageFont

output_dir = os.path.join(os.path.dirname(__file__), "..", "onboarding_site", "public", "images")
os.makedirs(output_dir, exist_ok=True)

font_bold = "C:/Windows/Fonts/segoeuib.ttf"
font_regular = "C:/Windows/Fonts/segoeui.ttf"
font_semibold = "C:/Windows/Fonts/seguisb.ttf"

def draw_squircle_ecg(draw, x, y, size, bg_color, stroke_color, stroke_w=6):
    r = int(size * 0.22)
    draw.rounded_rectangle([x, y, x + size, y + size], radius=r, fill=bg_color)
    
    # ECG Pulse points relative to size
    pts = [
        (x + size * 0.15, y + size * 0.50),
        (x + size * 0.30, y + size * 0.50),
        (x + size * 0.38, y + size * 0.28),
        (x + size * 0.47, y + size * 0.72),
        (x + size * 0.54, y + size * 0.38),
        (x + size * 0.61, y + size * 0.58),
        (x + size * 0.68, y + size * 0.50),
        (x + size * 0.85, y + size * 0.50)
    ]
    for i in range(len(pts) - 1):
        draw.line([pts[i], pts[i+1]], fill=stroke_color, width=stroke_w)

def generate_og_image():
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), color="#0B132B")
    draw = ImageDraw.Draw(img)
    
    # Subtle gradient background simulation
    for i in range(H):
        ratio = i / H
        r = int(11 + ratio * (28 - 11))
        g = int(19 + ratio * (37 - 19))
        b = int(43 + ratio * (65 - 43))
        draw.line([(0, i), (W, i)], fill=(r, g, b))
    
    # Decorative subtle circles
    draw.ellipse([850, -50, 1350, 450], outline="#162A45", width=2)
    draw.ellipse([920, 20, 1280, 380], outline="#00A88422", width=3)
    draw.ellipse([980, 80, 1220, 320], fill="#00A88415")
    
    # Draw large decorative logo mark on the right
    draw_squircle_ecg(draw, 930, 130, 240, "#00A88422", "#00A884", stroke_w=10)
    
    # Left brand header
    draw_squircle_ecg(draw, 80, 75, 84, "#00A884", "#FFFFFF", stroke_w=5)
    
    f_title = ImageFont.truetype(font_bold, 60)
    f_sub = ImageFont.truetype(font_semibold, 26)
    f_desc = ImageFont.truetype(font_regular, 24)
    f_pill = ImageFont.truetype(font_semibold, 20)
    f_cta = ImageFont.truetype(font_bold, 20)
    
    draw.text((185, 80), "Braga Saúde", font=f_title, fill="#FFFFFF")
    draw.text((188, 148), "Cuidado Diário & Família Conectada", font=f_sub, fill="#00A884")
    
    # Main value proposition headline
    f_headline = ImageFont.truetype(font_bold, 38)
    draw.text((80, 215), "App de Controle de Pressão, Glicemia", font=f_headline, fill="#F8FAFC")
    draw.text((80, 265), "e Cuidado Familiar com IA", font=f_headline, fill="#00A884")
    
    draw.text((80, 335), "Monitore sinais vitais diários, rotina de remédios e receba alertas", font=f_desc, fill="#94A3B8")
    draw.text((80, 370), "automáticos para os familiares e cuidadores pelo WhatsApp.", font=f_desc, fill="#94A3B8")
    
    # Feature Pills
    pills = [
        "✓ Alertas no WhatsApp da Família",
        "✓ Diretrizes SBC, SBD e OMS",
        "✓ Copiloto Clínico Braga IA"
    ]
    px = 80
    py = 435
    for pill in pills:
        bbox = draw.textbbox((0, 0), pill, font=f_pill)
        pw = bbox[2] - bbox[0] + 32
        draw.rounded_rectangle([px, py, px + pw, py + 42], radius=21, fill="#1E293B", outline="#334155")
        draw.text((px + 16, py + 9), pill, font=f_pill, fill="#38BDF8" if "WhatsApp" in pill else ("#34D399" if "SBC" in pill else "#A78BFA"))
        px += pw + 16
        
    # Footer Banner / CTA
    draw.rounded_rectangle([80, 520, 1120, 575], radius=14, fill="#00A884")
    cta_text = "📲 Baixe o Aplicativo Android Oficial Grátis  •  bragasaude.online"
    bbox_cta = draw.textbbox((0, 0), cta_text, font=f_cta)
    cta_w = bbox_cta[2] - bbox_cta[0]
    draw.text((80 + (1040 - cta_w) // 2, 535), cta_text, font=f_cta, fill="#FFFFFF")
    
    target_path = os.path.join(output_dir, "og-image.jpg")
    img.save(target_path, "JPEG", quality=92, optimize=True)
    print(f"Generated {target_path} ({os.path.getsize(target_path)} bytes)")

def generate_logo_and_icons():
    # Master logo 512x512
    size = 512
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw_squircle_ecg(draw, 16, 16, 480, "#00A884", "#FFFFFF", stroke_w=24)
    logo_path = os.path.join(output_dir, "logo.png")
    img.save(logo_path, "PNG")
    print(f"Generated {logo_path}")
    
    # Apple touch icon 180x180
    apple_img = img.resize((180, 180), Image.Resampling.LANCZOS)
    apple_path = os.path.join(output_dir, "apple-touch-icon.png")
    apple_img.save(apple_path, "PNG")
    print(f"Generated {apple_path}")
    
    # Favicon 32x32
    fav32 = img.resize((32, 32), Image.Resampling.LANCZOS)
    fav32_path = os.path.join(output_dir, "favicon-32x32.png")
    fav32.save(fav32_path, "PNG")
    print(f"Generated {fav32_path}")
    
    # Favicon 16x16
    fav16 = img.resize((16, 16), Image.Resampling.LANCZOS)
    fav16_path = os.path.join(output_dir, "favicon-16x16.png")
    fav16.save(fav16_path, "PNG")
    print(f"Generated {fav16_path}")

if __name__ == "__main__":
    generate_og_image()
    generate_logo_and_icons()
