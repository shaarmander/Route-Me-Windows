-- 80 byte packet; each column contains eight black/white bits, least significant first.
-- Header: repeating magenta/cyan/yellow/blue cells. No input or file/network access.
WayfinderOptions = type(WayfinderOptions)=="table" and WayfinderOptions or {}
local cellSize = WayfinderOptions.large and 4 or 2
local panel = CreateFrame("Frame", "WayfinderPanel", UIParent)
panel:SetFrameStrata("TOOLTIP")
panel:SetFrameLevel(100)
panel:SetSize(80*cellSize, 9*cellSize)
panel:EnableMouse(false)
local colors = {{1,0,1}, {0,1,1}, {1,1,0}, {0,0,1}}
local cells = {}
local headers = {}
for col = 0, 79 do
    local header = panel:CreateTexture(nil, "OVERLAY")
    header:SetSize(cellSize, cellSize)
    header:SetPoint("TOPLEFT", col*cellSize, 0)
    header:SetColorTexture(unpack(colors[col%4+1]))
    headers[col+1] = header
    cells[col+1] = {}
    for bit = 0, 7 do
        local cell = panel:CreateTexture(nil, "OVERLAY")
        cell:SetSize(cellSize, cellSize)
        cell:SetPoint("TOPLEFT", col*cellSize, -cellSize-bit*cellSize)
        cell:SetColorTexture(0, 0, 0, 1)
        cells[col+1][bit+1] = cell
    end
end
local function anchor()
    panel:SetScale(1/UIParent:GetEffectiveScale())
    panel:ClearAllPoints()
    panel:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 4, -4)
end
anchor()
panel:RegisterEvent("UI_SCALE_CHANGED")
panel:SetScript("OnEvent", anchor)
local sequence, elapsed = 0, 0
local function packet()
    local data = {}
    for i=1,80 do data[i]=0 end
    local function u16(i, n)
        n = math.max(0, math.min(65535, math.floor(n+.5)))
        data[i], data[i+1] = n%256, math.floor(n/256)
    end
    local function u32(i,n)
        n = math.floor(n+.5)%4294967296
        for j=0,3 do data[i+j]=math.floor(n/256^j)%256 end
    end
    data[1], data[2], data[3] = 82, 77, 4
    sequence = (sequence+1)%65536
    u16(5, sequence)
    local ok = pcall(function()
        local map = C_Map.GetBestMapForUnit("player")
        local info = map and C_Map.GetMapInfo(map)
        local pos = map and C_Map.GetPlayerMapPosition(map, "player")
        local facing = GetPlayerFacing and GetPlayerFacing()
        if not pos or not info or type(facing)~="number" then return end
        local x,y = pos:GetXY()
        if x<0 or x>1 or y<0 or y>1 then return end
        u16(7, map)
        u16(9, x*65535)
        u16(11, y*65535)
        -- WoW heading is counterclockwise from north; desktop uses clockwise.
        u16(13, ((-facing*180/math.pi)%360)*100)
        local name = info.name or ""
        for i=1,math.min(24,#name) do data[16+i]=string.byte(name,i) end
        data[4] = 1
        if IsStealthed and IsStealthed() then data[4] = 3 end
        -- Optional world telemetry has its own validity bit. Restricted or
        -- unavailable APIs must never make zero coordinates appear valid.
        pcall(function()
            if not UnitPosition then return end
            local wx,wy,wz,instance = UnitPosition("player")
            if type(wx)~="number" or type(wy)~="number" or type(wz)~="number" or type(instance)~="number" then return end
            if wx~=wx or wy~=wy or wz~=wz or math.abs(wx)>100000 or math.abs(wy)>100000 or math.abs(wz)>100000 then return end
            if instance<0 or instance>65535 then return end
            u32(41,wx*100); u32(45,wy*100); u32(49,wz*100)
            u16(53,instance)
            if GetBuildInfo then
                local _,build=GetBuildInfo()
                u32(55,tonumber(build) or 0)
            end
            data[4]=data[4]+4
        end)
    end)
    if not ok then data[4]=0 end
    -- Read-only map geometry, in physical pixels relative to the game client.
    -- Roads and clicks are handled by the external desktop overlay.
    pcall(function()
        local map = WorldMapFrame
        if not map or not map:IsVisible() or not map.GetCanvas or not GetPhysicalScreenSize then return end
        local _,screenHeight = GetPhysicalScreenSize()
        local function rectangle(frame)
            -- WoW layout units use a 768-high root coordinate space. Effective
            -- scale alone does not convert them to physical window pixels.
            local scale = frame:GetEffectiveScale()*screenHeight/768
            local left,top = frame:GetLeft(),frame:GetTop()
            if not left or not top then return end
            local r = {left*scale,screenHeight-top*scale,
                       (left+frame:GetWidth())*scale,screenHeight-(top-frame:GetHeight())*scale}
            for _,v in ipairs(r) do
                if v~=v or v < -32768 or v > 32767 then return end
            end
            if r[3]<=r[1] or r[4]<=r[2] then return end
            return r
        end
        local canvas = rectangle(map:GetCanvas())
        local clip = rectangle(map.ScrollContainer)
        local id = map:GetMapID()
        if not canvas or not clip or not id or id<1 or id>65535 then return end
        u16(59,id)
        for i=1,4 do
            u16(59+i*2,math.floor(canvas[i]+.5)%65536)
            u16(67+i*2,math.floor(clip[i]+.5)%65536)
        end
        data[4]=data[4]+8
    end)
    local sum, weighted = 0,0
    for i=1,78 do sum=(sum+data[i])%251; weighted=(weighted+i*data[i])%251 end
    data[79],data[80]=sum,weighted
    for col=1,80 do
        for bit=0,7 do
            local value = math.floor(data[col]/2^bit)%2
            cells[col][bit+1]:SetColorTexture(value,value,value,1)
        end
    end
end
panel:SetScript("OnUpdate", function(self, delta)
    elapsed = elapsed+delta
    if elapsed>=.05 then elapsed=0; packet() end
end)
SLASH_WAYFINDER1 = "/wayfinder"
SlashCmdList.WAYFINDER = function(message)
    local option = (message or ""):lower():match("^%s*(.-)%s*$")
    if option=="large" or option=="small" then
        WayfinderOptions.large = option=="large"
        cellSize = WayfinderOptions.large and 4 or 2
        panel:SetSize(80*cellSize, 9*cellSize)
        for col=1,80 do
            headers[col]:SetSize(cellSize, cellSize)
            headers[col]:ClearAllPoints()
            headers[col]:SetPoint("TOPLEFT", (col-1)*cellSize, 0)
            for bit=1,8 do
                cells[col][bit]:SetSize(cellSize, cellSize)
                cells[col][bit]:ClearAllPoints()
                cells[col][bit]:SetPoint("TOPLEFT", (col-1)*cellSize, -bit*cellSize)
            end
        end
    elseif option~="" then
        print("Wayfinder: /wayfinder [small|large]. Roads and destination clicks are shown in the Route Me desktop overlay.")
        return
    end
    panel:Show(); anchor()
end
