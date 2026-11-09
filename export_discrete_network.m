function output_file = export_discrete_network(city,demand,mu,alpha,citysize)
city = string(city);
assert(isscalar(city) && ~isempty(regexp(char(city),'^[A-Za-z][A-Za-z0-9_]*$','once')), 'Invalid city name.');
validateattributes(demand,{'numeric'},{'scalar','real','finite','integer','positive'});
validateattributes(mu,{'numeric'},{'scalar','real','finite','positive'});
validateattributes(alpha,{'numeric'},{'scalar','real','finite','nonnegative'});
validateattributes(citysize,{'numeric'},{'scalar','real','finite','positive'});
project_directory = java.io.File(fileparts(mfilename('fullpath')));
root = string(project_directory.getCanonicalPath());
scenario = struct('city',city,'demand',demand,'mu',mu,'alpha',alpha,'citysize',citysize);
key = scenario_key(scenario);
source_name = extractAfter(key,5)+".mat";
scenario.source_reference = "inputs/model/"+source_name;
scenario.source = project_file(root,fullfile(root,"inputs","model",source_name));
output_root = project_file(root,fullfile(root,"outputs","network"));
output_file = project_file(root,fullfile(output_root,"network_"+key+".xlsx"));
network = discretize_solution(scenario);
if ~isfolder(output_root), mkdir(output_root); end
temporary_file = project_file(root,string(tempname(output_root))+".xlsx");
cleanup = onCleanup(@() delete_temporary(temporary_file));
sheet_names = fieldnames(network);
for sheet_index = 1:numel(sheet_names)
    sheet_name = sheet_names{sheet_index};
    write_split_table(network.(sheet_name),temporary_file,sheet_name);
end
[ok,message] = movefile(temporary_file,output_file,'f');
assert(ok,'%s',message);
end

function path = project_file(root,path)
file = java.io.File(char(path));
path = string(file.getCanonicalPath());
assert(startsWith(path,root+filesep,'IgnoreCase',ispc),'Path is outside the project.');
end

function delete_temporary(path)
if isfile(path), delete(path); end
end

function key = scenario_key(scenario)
key = "city_"+string(scenario.city)+"_demand_"+compose('%.12g',scenario.demand)+ ...
    "_mu_"+compose('%.12g',scenario.mu)+"_alpha_"+compose('%.12g',scenario.alpha)+ ...
    "_citysize_"+compose('%.12g',scenario.citysize);
end

function network = discretize_solution(scenario)
solution = load(scenario.source);
required = {'delta_EW','delta_NS','h_E','h_W','h_N','h_S'};
for field_index = 1:numel(required)
    if ~isfield(solution,required{field_index}), error('simulation:MissingField','解缺字段 %s',required{field_index}); end
    values = solution.(required{field_index});
    if any(~isfinite(values(:))) || any(values(:)<=0), error('simulation:InvalidField','场 %s 必须严格正且有限。',required{field_index}); end
end
citysize = scenario.citysize;
[nx,ny] = size(solution.delta_EW);
for field_index = 1:numel(required)
    if ~isequal(size(solution.(required{field_index})),[nx,ny]), error('simulation:FieldShape','六个场的网格尺寸必须一致。'); end
end
x_grid = linspace(0,citysize,nx);
y_grid = linspace(0,citysize,ny);
east_density = (solution.delta_EW(1:end-1,:)+solution.delta_EW(2:end,:))/2;
north_density = ((solution.delta_NS(:,1:end-1)+solution.delta_NS(:,2:end))/2)';
[east_positions,east_geometry] = build_positions(x_grid,y_grid,east_density,true);
[north_positions,north_geometry] = build_positions(y_grid,x_grid,north_density,false);
geometry = [east_geometry;north_geometry];
north_offset = size(east_geometry,1);
[stations,geometry_stops] = intersect_stations(east_geometry,north_geometry);
directions = ["E","W","N","S"];
route_rows = zeros(0,3);
route_directions = strings(0,1);
segment_cells = {};
segment_direction_cells = {};
segment_kind_cells = {};
route_segment_cells = {};
route_stop_cells = {};
next_segment_id = 1;
adjustment_rows = zeros(0,4);
adjustment_directions = strings(0,1);
for direction_index = 1:4
    direction = directions(direction_index);
    if direction_index<=2
        density = solution.delta_EW;
        q_grid = density./solution.(['h_',char(direction)]);
        section_q = (q_grid(1:end-1,:)+q_grid(2:end,:))/2;
        cross_grid = y_grid;
        positions = east_positions;
        geometry_offset = 0;
    else
        density = solution.delta_NS;
        q_grid = density./solution.(['h_',char(direction)]);
        section_q = ((q_grid(:,1:end-1)+q_grid(:,2:end))/2)';
        cross_grid = x_grid;
        positions = north_positions;
        geometry_offset = north_offset;
    end
    section_totals = trapz(cross_grid,section_q,2);
    target_total = mean(section_totals);
    relative_adjustments = abs(target_total-section_totals)./section_totals;
    adjustment_rows = [adjustment_rows;[(1:numel(section_totals))',section_totals,repmat(target_total,numel(section_totals),1),relative_adjustments]];
    adjustment_directions = [adjustment_directions;repmat(direction,numel(section_totals),1)];
    if any(relative_adjustments>0.05)
        error('simulation:FlowAdjustment','输入异常：%s 方向截面频率相对修正 %.3f%% 超过 5%%。',direction,100*max(relative_adjustments));
    end
    [boundaries,line_offsets] = frequency_boundaries(cross_grid,section_q,positions,section_totals,target_total);
    cuts = unique([0,target_total,cell2mat(boundaries)]);
    for cut_index = 1:numel(cuts)-1
        frequency = cuts(cut_index+1)-cuts(cut_index);
        if frequency<=0, continue; end
        quantile = cuts(cut_index)+frequency/2;
        chosen_geometry = zeros(1,numel(positions));
        for section_index = 1:numel(positions)
            line_index = find(boundaries{section_index}(2:end)>=quantile,1);
            if isempty(line_index), line_index=numel(positions{section_index}); end
            chosen_geometry(section_index) = geometry_offset+line_offsets(section_index)+line_index;
        end
        reversed = direction=="W" || direction=="S";
        if reversed, chosen_geometry=fliplr(chosen_geometry); end
        route_id = size(route_rows,1)+1;
        cumulative_distance = 0;
        previous_end = [];
        current_stops = zeros(0,2);
        current_segments = zeros(0,7);
        current_segment_kinds = strings(0,1);
        sequence = 0;
        for geometry_index = chosen_geometry
            main_geometry = geometry(geometry_index,:);
            if reversed, main_geometry=main_geometry([3,4,1,2]); end
            if ~isempty(previous_end) && norm(previous_end-main_geometry(1:2),1)>1e-10
                detour_geometry = [previous_end,main_geometry(1:2)];
                detour_length = sum(abs(detour_geometry(3:4)-detour_geometry(1:2)));
                segment_id = next_segment_id; next_segment_id=next_segment_id+1;
                current_segments(end+1,:) = [segment_id,detour_geometry,detour_length,frequency];
                current_segment_kinds(end+1,1)="detour";
                sequence=sequence+1;
                cumulative_distance=cumulative_distance+detour_length;
            end
            main_length = sum(abs(main_geometry(3:4)-main_geometry(1:2)));
            segment_id = next_segment_id; next_segment_id=next_segment_id+1;
            current_segments(end+1,:)=[segment_id,main_geometry,main_length,frequency];
            current_segment_kinds(end+1,1)="main";
            sequence=sequence+1;
            stop_ids = geometry_stops{geometry_index};
            if ~isempty(stop_ids)
                stop_distances = abs(stations.x_km(stop_ids)-main_geometry(1))+abs(stations.y_km(stop_ids)-main_geometry(2));
                [stop_distances,stop_order]=sort(stop_distances);
                current_stops=[current_stops;[stop_ids(stop_order),cumulative_distance+stop_distances]];
            end
            cumulative_distance=cumulative_distance+main_length;
            previous_end=main_geometry(3:4);
        end
        if ~isempty(current_stops)
            keep = [true;~(diff(current_stops(:,1))==0 & abs(diff(current_stops(:,2)))<1e-9)];
            current_stops=current_stops(keep,:);
        end
        segment_cells{route_id,1}=current_segments;
        segment_direction_cells{route_id,1}=repmat(direction,size(current_segments,1),1);
        segment_kind_cells{route_id,1}=current_segment_kinds;
        route_segment_cells{route_id,1}=[repmat(route_id,sequence,1),(1:sequence)',current_segments(:,1)];
        route_stop_cells{route_id,1}=[repmat(route_id,size(current_stops,1),1),(1:size(current_stops,1))',current_stops];
        route_rows(end+1,:)=[route_id,frequency,cumulative_distance];
        route_directions(end+1,1)=direction;
    end
    direction_frequency = sum(route_rows(route_directions==direction,2));
    assert(abs(direction_frequency-target_total)<=1e-8*max(1,target_total),'完整路径分解未保持截面频率。');
end
segment_rows=vertcat(segment_cells{:});
segment_directions=vertcat(segment_direction_cells{:});
segment_kinds=vertcat(segment_kind_cells{:});
route_segments_rows=vertcat(route_segment_cells{:});
route_stops_rows=vertcat(route_stop_cells{:});
clear segment_cells segment_direction_cells segment_kind_cells route_segment_cells route_stop_cells
[unique_geometry,~,segment_group] = unique([double(char(segment_directions)),double(segment_kinds=="detour"),round(segment_rows(:,2:5)*1e10)/1e10],'rows','stable');
first_occurrences = accumarray(segment_group,(1:numel(segment_group))',[],@min);
merged_frequency = accumarray(segment_group,segment_rows(:,7));
segment_rows = [(1:size(unique_geometry,1))',segment_rows(first_occurrences,2:6),merged_frequency];
segment_directions=segment_directions(first_occurrences); segment_kinds=segment_kinds(first_occurrences);
route_segments_rows(:,3)=segment_group(route_segments_rows(:,3));
network = struct();
metadata_keys = ["schema_version";"city";"demand";"mu";"alpha";"citysize";"source";"coordinate_unit";"frequency_unit";"station_rule";"flow_input_status";"interpolation";"max_relative_flow_adjustment";"includes_export_completed"];
metadata_values = ["1";string(scenario.city);compose('%.12g',scenario.demand);compose('%.12g',scenario.mu);compose('%.12g',scenario.alpha);compose('%.12g',citysize);scenario.source_reference;"km";"veh/h";"EW_NS_main_intersections_only";"valid";"cubic_spline_nonnegative_trapezoid_mass_calibrated";compose('%.12g',max(adjustment_rows(:,4)));"true"];
network.metadata=table(metadata_keys,metadata_values,'VariableNames',{'key','value'});
network.stations=stations;
network.nodes=table(stations.station_id,stations.x_km,stations.y_km,'VariableNames',{'node_id','x_km','y_km'});
network.segments=table(segment_rows(:,1),segment_directions,segment_kinds,segment_rows(:,2),segment_rows(:,3),segment_rows(:,4),segment_rows(:,5),segment_rows(:,6),segment_rows(:,7),'VariableNames',{'segment_id','direction','kind','x1_km','y1_km','x2_km','y2_km','length_km','frequency_veh_h'});
network.routes=table(route_rows(:,1),route_directions,route_rows(:,2),route_rows(:,3),'VariableNames',{'route_id','direction','frequency_veh_h','length_km'});
network.route_segments=array2table(route_segments_rows,'VariableNames',{'route_id','sequence','segment_id'});
network.route_stops=array2table(route_stops_rows,'VariableNames',{'route_id','sequence','station_id','distance_km'});
network.flow_adjustments=table(adjustment_directions,adjustment_rows(:,1),adjustment_rows(:,2),adjustment_rows(:,3),adjustment_rows(:,4),'VariableNames',{'direction','section_id','raw_total','adjusted_total','relative_adjustment'});
validate_network(network,citysize);
end

function [positions,geometry] = build_positions(main_grid,cross_grid,density,is_horizontal)
positions=cell(1,size(density,1));
geometry=zeros(0,4);
for section_index=1:size(density,1)
    [fine_grid,cumulative]=calibrated_cumulative(cross_grid,density(section_index,:));
    levels=1:floor(cumulative(end)+1e-10);
    if isempty(levels)
        error('simulation:NoDiscreteLine','第 %d 主向区间总密度不足一条线路，不能构造守恒网络。',section_index);
    end
    transverse_positions=interp1(cumulative,fine_grid,min(levels,cumulative(end)),'linear');
    positions{section_index}=transverse_positions;
    count=numel(transverse_positions);
    if is_horizontal
        geometry=[geometry;repmat(main_grid(section_index),count,1),transverse_positions',repmat(main_grid(section_index+1),count,1),transverse_positions'];
    else
        geometry=[geometry;transverse_positions',repmat(main_grid(section_index),count,1),transverse_positions',repmat(main_grid(section_index+1),count,1)];
    end
end
end

function [fine_grid,cumulative] = calibrated_cumulative(grid,values)
fine_grid=linspace(grid(1),grid(end),max(10001,1000*(numel(grid)-1)+1));
fine_values=max(0,ppval(spline(grid,values),fine_grid));
reference_mass=trapz(grid,values);
interpolated_mass=trapz(fine_grid,fine_values);
if interpolated_mass<=0 || reference_mass<=0, error('simulation:ZeroMass','密度或频率积分必须为正。'); end
fine_values=fine_values*(reference_mass/interpolated_mass);
cumulative=cumtrapz(fine_grid,fine_values);
cumulative(end)=reference_mass;
end

function [boundaries,line_offsets] = frequency_boundaries(cross_grid,section_q,positions,section_totals,target_total)
boundaries=cell(1,numel(positions));
line_offsets=zeros(1,numel(positions));
for section_index=1:numel(positions)
    [fine_grid,cumulative]=calibrated_cumulative(cross_grid,section_q(section_index,:));
    line_cumulative=interp1(fine_grid,cumulative,positions{section_index},'linear');
    line_cumulative=line_cumulative*(target_total/section_totals(section_index));
    line_cumulative(end)=target_total;
    boundaries{section_index}=[0,line_cumulative];
    if section_index>1, line_offsets(section_index)=line_offsets(section_index-1)+numel(positions{section_index-1}); end
end
end

function [stations,geometry_stops] = intersect_stations(horizontal,vertical)
coordinates=zeros(0,2);
intersection_pairs=zeros(0,2);
for horizontal_index=1:size(horizontal,1)
    current=horizontal(horizontal_index,:);
    mask=vertical(:,1)>=current(1)-1e-10 & vertical(:,1)<=current(3)+1e-10 & current(2)>=vertical(:,2)-1e-10 & current(2)<=vertical(:,4)+1e-10;
    vertical_indices=find(mask);
    coordinates=[coordinates;vertical(mask,1),repmat(current(2),numel(vertical_indices),1)];
    intersection_pairs=[intersection_pairs;repmat(horizontal_index,numel(vertical_indices),1),size(horizontal,1)+vertical_indices];
end
[unique_coordinates,~,station_groups]=unique(round(coordinates*1e10)/1e10,'rows');
stations=table((1:size(unique_coordinates,1))',unique_coordinates(:,1),unique_coordinates(:,2),'VariableNames',{'station_id','x_km','y_km'});
geometry_stops=cell(size(horizontal,1)+size(vertical,1),1);
for intersection_index=1:size(intersection_pairs,1)
    station_id=station_groups(intersection_index);
    for geometry_index=intersection_pairs(intersection_index,:)
        geometry_stops{geometry_index}(end+1,1)=station_id;
    end
end
for geometry_index=1:numel(geometry_stops), geometry_stops{geometry_index}=unique(geometry_stops{geometry_index}); end
end

function validate_network(network,citysize)
segments=network.segments; routes=network.routes;
coordinates=[segments.x1_km;segments.y1_km;segments.x2_km;segments.y2_km];
assert(all(coordinates>=-1e-8 & coordinates<=citysize+1e-8),'线段坐标超出城市边界。');
segment_counts=accumarray(network.route_segments.route_id,1,[height(routes),1]);
segment_offsets=[0;cumsum(segment_counts)];
stop_counts=accumarray(network.route_stops.route_id,1,[height(routes),1]);
stop_offsets=[0;cumsum(stop_counts)];
for route_index=1:height(routes)
    rows=network.route_segments(segment_offsets(route_index)+1:segment_offsets(route_index+1),:);
    route_geometry=segments(rows.segment_id,:);
    gaps=abs(route_geometry.x2_km(1:end-1)-route_geometry.x1_km(2:end))+abs(route_geometry.y2_km(1:end-1)-route_geometry.y1_km(2:end));
    assert(all(gaps<1e-8),'完整路径存在不连续线段。');
    stops=network.route_stops(stop_offsets(route_index)+1:stop_offsets(route_index+1),:);
    assert(all(diff(stops.distance_km)>=-1e-8),'站点未按车辆行驶距离排序。');
    assert(all(stops.distance_km<=routes.length_km(route_index)+1e-8),'站点累计距离超过完整线路长度。');
    assert(abs(sum(route_geometry.length_km)-routes.length_km(route_index))<1e-7,'路径长度不一致。');
end
end

function write_split_table(data,output_file,base_name)
rows_per_sheet=500000;
if height(data)<=rows_per_sheet
    writetable(data,output_file,'Sheet',base_name,'UseExcel',false);
else
    for part_index=1:ceil(height(data)/rows_per_sheet)
        first_row=(part_index-1)*rows_per_sheet+1;
        last_row=min(part_index*rows_per_sheet,height(data));
        writetable(data(first_row:last_row,:),output_file,'Sheet',sprintf('%s_%03d',base_name,part_index),'UseExcel',false);
    end
end
end
