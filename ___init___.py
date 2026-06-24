bl_info = {
    "name": "VSE Freesound Integration",
    "author": "Gemini",
    "version": (1, 0),
    "blender": (5, 2, 0),
    "location": "Sequence Editor > UI > Freesound",
    "description": "Search, preview, and import Freesound audio directly into the VSE",
    "category": "Sequencer",
}

import bpy
import urllib.request
import urllib.parse
import json
import os
import tempfile
import subprocess
import aud

# Global variable to handle audio preview playback
_preview_handle = None

class FreesoundResultItem(bpy.types.PropertyGroup):
    """Stores individual search results"""
    sound_id: bpy.props.StringProperty()
    name: bpy.props.StringProperty()
    author: bpy.props.StringProperty()
    description: bpy.props.StringProperty()
    preview_url: bpy.props.StringProperty()
    download_url: bpy.props.StringProperty()

class VSE_Freesound_Settings(bpy.types.PropertyGroup):
    api_key: bpy.props.StringProperty(
        name="API Key",
        description="Your Freesound API Key (Token)",
        default="",
        subtype='PASSWORD'
    )
    search_query: bpy.props.StringProperty(
        name="Search",
        description="Search Freesound",
        default=""
    )
    sort_by: bpy.props.EnumProperty(
        name="Sort By",
        items=[
            ('score', "Relevance", ""),
            ('rating_desc', "Highest Rated", ""),
            ('downloads_desc', "Most Downloaded", ""),
            ('created_desc', "Newest", "")
        ],
        default='score'
    )
    results: bpy.props.CollectionProperty(type=FreesoundResultItem)
    current_index: bpy.props.IntProperty(default=0)
    
    # Download Parameters
    compression_level: bpy.props.IntProperty(
        name="Compression %",
        description="0% = Disabled. 1-100% scales the audio bitrate (100% = heavily compressed)",
        default=0,
        min=0,
        max=100
    )
    normalize_volume: bpy.props.BoolProperty(
        name="Normalize Volume",
        description="Use FFmpeg loudnorm filter to normalize volume",
        default=False
    )

class VSE_OT_Freesound_Search(bpy.types.Operator):
    bl_idname = "vse.freesound_search"
    bl_label = "Search Freesound"
    bl_description = "Execute search on Freesound API"

    def execute(self, context):
        settings = context.scene.freesound_settings
        if not settings.api_key:
            self.report({'ERROR'}, "Please enter your Freesound API Key.")
            return {'CANCELLED'}

        query = urllib.parse.quote(settings.search_query)
        url = f"https://freesound.org/apiv2/search/text/?query={query}&sort={settings.sort_by}&fields=id,name,username,description,previews&page_size=15"
        
        req = urllib.request.Request(url)
        req.add_header('Authorization', f'Token {settings.api_key}')
        
        try:
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())
                
                settings.results.clear()
                settings.current_index = 0
                
                for item in data.get('results', []):
                    res = settings.results.add()
                    res.sound_id = str(item.get('id', ''))
                    res.name = item.get('name', 'Unknown')
                    res.author = item.get('username', 'Unknown')
                    
                    # Clean and truncate description
                    desc = item.get('description', 'No description.')
                    res.description = desc[:200] + "..." if len(desc) > 200 else desc
                    
                    previews = item.get('previews', {})
                    res.preview_url = previews.get('preview-lq-mp3', '')
                    res.download_url = previews.get('preview-hq-mp3', previews.get('preview-hq-ogg', ''))
                    
            self.report({'INFO'}, f"Found {len(settings.results)} results.")
        except Exception as e:
            self.report({'ERROR'}, f"Search failed: {str(e)}")
            
        return {'FINISHED'}

class VSE_OT_Freesound_Navigate(bpy.types.Operator):
    bl_idname = "vse.freesound_navigate"
    bl_label = "Navigate Results"
    
    direction: bpy.props.IntProperty()

    def execute(self, context):
        settings = context.scene.freesound_settings
        total = len(settings.results)
        if total > 0:
            settings.current_index = (settings.current_index + self.direction) % total
            
            # Stop preview if navigating
            global _preview_handle
            if _preview_handle:
                _preview_handle.stop()
                _preview_handle = None
                
        return {'FINISHED'}

class VSE_OT_Freesound_Preview(bpy.types.Operator):
    bl_idname = "vse.freesound_preview"
    bl_label = "Preview Audio"
    
    def execute(self, context):
        global _preview_handle
        settings = context.scene.freesound_settings
        
        if len(settings.results) == 0:
            return {'CANCELLED'}
            
        item = settings.results[settings.current_index]
        if not item.preview_url:
            self.report({'ERROR'}, "No preview available for this sound.")
            return {'CANCELLED'}
            
        # Stop existing preview
        if _preview_handle:
            _preview_handle.stop()
            _preview_handle = None

        try:
            # Download temp preview
            temp_dir = tempfile.gettempdir()
            temp_file = os.path.join(temp_dir, f"fs_preview_{item.sound_id}.mp3")
            
            if not os.path.exists(temp_file):
                req = urllib.request.Request(item.preview_url)
                with urllib.request.urlopen(req) as response, open(temp_file, 'wb') as out_file:
                    out_file.write(response.read())
            
            # Play using Blender's aud module
            device = aud.Device()
            sound = aud.Sound(temp_file)
            _preview_handle = device.play(sound)
            
        except Exception as e:
            self.report({'ERROR'}, f"Preview failed: {str(e)}")

        return {'FINISHED'}

class VSE_OT_Freesound_Add(bpy.types.Operator):
    bl_idname = "vse.freesound_add"
    bl_label = "Add to VSE"
    
    def execute(self, context):
        settings = context.scene.freesound_settings
        
        if len(settings.results) == 0:
            return {'CANCELLED'}
            
        item = settings.results[settings.current_index]
        if not item.download_url:
            self.report({'ERROR'}, "No download link available.")
            return {'CANCELLED'}

        # Setup VSE Context
        if not context.scene.sequence_editor:
            context.scene.sequence_editor_create()
            
        try:
            # 1. Download HQ Preview to temp folder
            temp_dir = tempfile.gettempdir()
            raw_file = os.path.join(temp_dir, f"fs_raw_{item.sound_id}.mp3")
            final_file = os.path.join(temp_dir, f"fs_final_{item.sound_id}.mp3")
            
            req = urllib.request.Request(item.download_url)
            with urllib.request.urlopen(req) as response, open(raw_file, 'wb') as out_file:
                out_file.write(response.read())
            
            # 2. Process with FFmpeg if needed
            if settings.compression_level > 0 or settings.normalize_volume:
                cmd = ['ffmpeg', '-y', '-i', raw_file]
                
                # Normalization
                if settings.normalize_volume:
                    cmd.extend(['-filter:a', 'loudnorm'])
                
                # Compression (Map 1-100% to roughly 128k down to 16k bitrate)
                if settings.compression_level > 0:
                    bitrate = int(128 - (1.12 * settings.compression_level))
                    bitrate = max(16, bitrate) # floor at 16k
                    cmd.extend(['-b:a', f'{bitrate}k'])
                    
                cmd.append(final_file)
                
                # Run FFmpeg (requires ffmpeg in system PATH)
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                import_file = final_file
            else:
                import_file = raw_file

            # 3. Add to Sequence Editor at current frame
            channel = 1
            frame = context.scene.frame_current
            name_clean = "".join([c for c in item.name if c.isalpha() or c.isdigit() or c==' ']).rstrip()
            
            context.scene.sequence_editor.sequences.new_sound(
                name=name_clean,
                filepath=import_file,
                channel=channel,
                frame_start=frame
            )
            
            self.report({'INFO'}, f"Added {item.name} to VSE")
            
        except FileNotFoundError:
            self.report({'ERROR'}, "FFmpeg not found. Please ensure FFmpeg is installed and in your system PATH.")
        except Exception as e:
            self.report({'ERROR'}, f"Failed to add sound: {str(e)}")

        return {'FINISHED'}


class VSE_PT_Freesound(bpy.types.Panel):
    bl_label = "Freesound Library"
    bl_space_type = 'SEQUENCE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Freesound"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.freesound_settings

        # Authentication & Search
        box = layout.box()
        box.prop(settings, "api_key", icon='KEY_HLT')
        box.prop(settings, "search_query", icon='VIEWZOOM')
        
        row = box.row(align=True)
        row.prop(settings, "sort_by", text="")
        row.operator("vse.freesound_search", text="Search", icon='FILE_REFRESH')

        # Results Browser
        if len(settings.results) > 0:
            layout.separator()
            item = settings.results[settings.current_index]
            
            # Navigation Header
            nav_row = layout.row(align=True)
            prev_op = nav_row.operator("vse.freesound_navigate", text="", icon='TRIA_LEFT')
            prev_op.direction = -1
            
            nav_row.label(text=f"Result {settings.current_index + 1} of {len(settings.results)}")
            
            next_op = nav_row.operator("vse.freesound_navigate", text="", icon='TRIA_RIGHT')
            next_op.direction = 1
            
            # Display Details
            res_box = layout.box()
            res_box.label(text=item.name, icon='SOUND')
            res_box.label(text=f"By: {item.author}", icon='USER')
            
            # Wrap description into chunks so it doesn't clip the panel bounds
            desc_col = res_box.column()
            words = item.description.split()
            line = ""
            for word in words:
                if len(line) + len(word) > 40:
                    desc_col.label(text=line)
                    line = word + " "
                else:
                    line += word + " "
            if line:
                desc_col.label(text=line)

            layout.operator("vse.freesound_preview", icon='PLAY')
            
            # Download / Add settings
            layout.separator()
            layout.label(text="Import Settings:")
            
            dl_box = layout.box()
            dl_box.prop(settings, "compression_level", slider=True)
            dl_box.prop(settings, "normalize_volume")
            
            dl_row = dl_box.row()
            dl_row.scale_y = 1.5
            dl_row.operator("vse.freesound_add", icon='IMPORT')

def register():
    bpy.utils.register_class(FreesoundResultItem)
    bpy.utils.register_class(VSE_Freesound_Settings)
    bpy.types.Scene.freesound_settings = bpy.props.PointerProperty(type=VSE_Freesound_Settings)
    
    bpy.utils.register_class(VSE_OT_Freesound_Search)
    bpy.utils.register_class(VSE_OT_Freesound_Navigate)
    bpy.utils.register_class(VSE_OT_Freesound_Preview)
    bpy.utils.register_class(VSE_OT_Freesound_Add)
    bpy.utils.register_class(VSE_PT_Freesound)

def unregister():
    bpy.utils.unregister_class(VSE_PT_Freesound)
    bpy.utils.unregister_class(VSE_OT_Freesound_Add)
    bpy.utils.unregister_class(VSE_OT_Freesound_Preview)
    bpy.utils.unregister_class(VSE_OT_Freesound_Navigate)
    bpy.utils.unregister_class(VSE_OT_Freesound_Search)
    
    del bpy.types.Scene.freesound_settings
    bpy.utils.unregister_class(VSE_Freesound_Settings)
    bpy.utils.unregister_class(FreesoundResultItem)

if __name__ == "__main__":
    register()
